from collections import defaultdict
from itertools import count
from time import time
from typing import Callable, Optional, Sequence

import numpy as np
import torch
import torch.nn.functional as F
from gym.spaces import Box
from stable_baselines3.common.vec_env import VecEnv
from torch import nn, optim

from neat_improved.rl.actor_critic.utils import explained_variance
from neat_improved.rl.reporters import BaseRLReporter
from neat_improved.trainer import BaseTrainer


class A2CTrainer(BaseTrainer):
    def __init__(
            self,
            policy: nn.Module,
            vec_envs: VecEnv,
            n_steps: int = 5,
            lr: float = 7e-4,
            use_scheduler: bool = False,
            eps: float = 1e-5,
            alpha: float = 0.99,
            max_grad_norm: float = 0.5,
            gamma: float = 0.99,
            value_loss_coef: float = 0.5,
            entropy_coef: float = 0.01,
            log_interval: int = 10000,
            normalize_advantage: bool = False,
            use_gpu: bool = True,
            critic_loss_func: Callable = F.mse_loss,
            reporters: Optional[Sequence[BaseRLReporter]] = None,
    ):
        super(A2CTrainer, self).__init__()

        self.device = 'cuda' if use_gpu else 'cpu'
        self.policy = policy.to(self.device)

        self.vec_envs = vec_envs
        self.action_space = self.vec_envs.action_space
        self.value_loss_coef = value_loss_coef
        self.entropy_coef = entropy_coef

        self.max_grad_norm = max_grad_norm
        self.gamma = gamma
        self.normalize_advantage = normalize_advantage

        self.optimizer = optim.RMSprop(
            self.policy.parameters(),
            lr,
            eps=eps,
            alpha=alpha,
        )
        self.use_scheduler = use_scheduler
        if use_scheduler:
            self._current_learn_progress_remaining = 1  # from 0 to 1 range, starts with 1
            self.lr_scheduler = torch.optim.lr_scheduler.LambdaLR(
                self.optimizer,
                lr_lambda=lambda x: self._current_learn_progress_remaining,
            )

        self.reporters = reporters or ()
        self.critic_loss_func = critic_loss_func
        self.log_interval = log_interval
        self.n_envs = self.vec_envs.num_envs
        self.n_steps = n_steps
        self.mini_batch = self.n_steps * self.n_envs
        self._num_frames = 0

    def _update_learn_progress_remaining(self, total_frames: int):
        self._current_learn_progress_remaining = 1. - float(self._num_frames) / float(total_frames)

    def _estimate_total_frames(self, update_duration, stop_time: int):
        return (stop_time // update_duration) * self.n_steps

    def _train(self, num_frames: Optional[int] = None, stop_time: Optional[int] = None):
        start_time = time()
        iter_ = count()

        state = self.vec_envs.reset()
        fitness_scores = [0] * self.vec_envs.num_envs

        fitness = 0.0
        for update in iter_:
            if stop_time and (time() - start_time) >= stop_time:
                break

            if self._num_frames and (self._num_frames >= num_frames):
                break

            (
                entropy,
                actor_loss,
                critic_loss,
                policy_loss,
                episode_end_fitness_scores,
                values,
                returns,
            ) = self.update(state, fitness_scores)
            n_seconds = time() - start_time

            if stop_time and not num_frames:
                num_frames = self._estimate_total_frames(
                    update_duration=n_seconds, stop_time=stop_time
                )
            self._update_learn_progress_remaining(total_frames=num_frames)

            if episode_end_fitness_scores:
                fitness = np.array(episode_end_fitness_scores).mean()

            self._call_reporters(
                'on_update_end',
                iteration=update,
                fitness=fitness,
                policy_loss=policy_loss.item(),
                num_frames=self._num_frames,
            )

            # Calculate the fps (frame per second)
            fps = int((update * self.mini_batch) / n_seconds)

            if (update % self.log_interval) == 0 or update == 1:
                ev = explained_variance(values, returns)

                total_num_steps = (update + 1) * self.n_envs * self.n_steps
                print(f"Updates: {update}, total env steps: {total_num_steps}, fps: {fps}")
                print(f"Entropy: {entropy:.4f}, policy loss: {policy_loss:.4f}")
                print(f"Explained variance: {float(ev):.4f}")
                print(f"Fitness: {fitness}")
                print(f"Optimizer lr: {self.optimizer.param_groups[0]['lr']}")
                print("---")

    def update(self, state, fitness_scores):
        buffer = defaultdict(list)
        entropy = 0.0
        episode_end_fitness_scores = []

        n = 0
        for _ in range(self.n_steps):
            state = torch.tensor(state, dtype=torch.float32, device=self.device)
            action, critic_values, action_log_probs, dist_entropy = self.policy(state)
            action = action.cpu().numpy()

            # Clip the actions to avoid out of bound error
            clipped_action = action
            if isinstance(self.action_space, Box):
                clipped_action = np.clip(action, self.action_space.low, self.action_space.high)
            else:
                clipped_action = clipped_action.flatten()

            # take action in env and look the results
            state, reward, done, infos = self.vec_envs.step(clipped_action)

            entropy += dist_entropy.sum()
            n += len(dist_entropy)

            for i, (r, d) in enumerate(zip(reward, done)):
                if not d:
                    fitness_scores[i] += r
                else:
                    episode_end_fitness_scores.append(fitness_scores[i])
                    fitness_scores[i] = 0.0

            self._num_frames += self.vec_envs.num_envs

            buffer['log_probs'].append(action_log_probs)
            buffer['values'].append(critic_values)
            buffer['rewards'].append(
                torch.tensor(
                    reward,
                    dtype=torch.float,
                    device=self.device,
                ).unsqueeze(1)
            )
            buffer['masks'].append(
                torch.tensor(
                    1 - done,
                    device=self.device,
                ).unsqueeze(1)
            )

        entropy /= n
        next_state = torch.tensor(
            state,
            dtype=torch.float,
            device=self.device,
        )
        next_value = self.policy.get_critic_values(next_state)
        returns = self.compute_returns(next_value, buffer['rewards'], buffer['masks'])

        log_probs = torch.cat(buffer['log_probs'])
        returns = torch.cat(returns).detach()
        values = torch.cat(buffer['values'])

        advantage = returns - values

        if self.normalize_advantage:
            advantage = (advantage - advantage.mean()) / (advantage.std() + 1e-8)

        actor_loss = -(log_probs * advantage.detach()).mean()
        critic_loss = self.critic_loss_func(returns, values)

        loss = actor_loss + self.value_loss_coef * critic_loss - self.entropy_coef * entropy

        self.optimizer.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(self.policy.parameters(), self.max_grad_norm)
        self.optimizer.step()

        if self.use_scheduler:
            self.lr_scheduler.step()

        return entropy, actor_loss, critic_loss, loss, episode_end_fitness_scores, values, returns

    def compute_returns(self, next_value, rewards, masks):
        r = next_value
        returns = []
        for step in reversed(range(len(rewards))):
            r = rewards[step] + self.gamma * r * masks[step]
            returns.insert(0, r)

        return returns

    def _call_reporters(self, stage: str, *args, **kwargs):
        for reporter in self.reporters:
            getattr(reporter, stage)(*args, **kwargs)
