from collections import defaultdict
from itertools import count
from pathlib import Path
from time import time
from typing import List, Optional, Tuple, Sequence

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from gym import Env
from torch.distributions import Categorical

from neat_improved.rl.reporters import BaseRLReporter
from neat_improved.trainer import BaseTrainer


class Actor(nn.Module):
    def __init__(self, state_size, action_size):
        super(Actor, self).__init__()
        self.state_size = state_size
        self.action_size = action_size
        self.linear1 = nn.Linear(self.state_size, 128)
        self.linear2 = nn.Linear(128, 256)
        self.linear3 = nn.Linear(256, self.action_size)

    def forward(self, state):
        output = F.relu(self.linear1(state))
        output = F.relu(self.linear2(output))
        output = self.linear3(output)
        distribution = Categorical(F.softmax(output, dim=-1))
        return distribution


class Critic(nn.Module):
    def __init__(self, state_size, action_size):
        super(Critic, self).__init__()
        self.state_size = state_size
        self.action_size = action_size
        self.linear1 = nn.Linear(self.state_size, 128)
        self.linear2 = nn.Linear(128, 256)
        self.linear3 = nn.Linear(256, 1)

    def forward(self, state):
        output = F.relu(self.linear1(state))
        output = F.relu(self.linear2(output))
        value = self.linear3(output)
        return value


class ActorCriticTrainer(BaseTrainer):
    def __init__(
        self,
        env: Env,
        actor: nn.Module,
        critic: nn.Module,
        stop_time: Optional[time] = None,
        lr: float = 0.001,
        gamma: float = 0.99,
        render: bool = False,
        save_dir: Optional[Path] = None,
        use_gpu: bool = True,
        reporters: Optional[Sequence[BaseRLReporter]] = None,
    ):
        self.lr = lr
        self.gamma = gamma
        self.render = render
        self.stop_time = stop_time
        self.save_dir = save_dir
        self.reporters = reporters or ()

        self.device = torch.device('cuda:0' if use_gpu else 'cpu')
        self.history = defaultdict(list)
        self.env = env
        self.actor = actor.to(self.device)
        self.critic = critic.to(self.device)
        self.optimizers = {
            'actor': optim.Adam(self.actor.parameters(), lr=self.lr),
            'critic': optim.Adam(self.critic.parameters(), lr=self.lr),
        }

    def _train(self, iterations: Optional[int]):
        start_time = time()
        iter_ = count() if iterations is None else range(iterations)

        for iteration in iter_:
            if self.stop_time and (time() - start_time) >= self.stop_time:
                break

            fitness = self._train_episode()
            self._call_reporters(
                'on_episode_end',
                iteration=iteration,
                fitness=fitness,
            )

        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            torch.save(self.actor, self.save_dir.joinpath('actor.pkl'))
            torch.save(self.critic, self.save_dir.joinpath('critic.pkl'))

        self.env.close()

    def _train_episode(self):
        state = self.env.reset()
        episode = defaultdict(list)
        fitness = 0.0

        for _ in count():
            if self.render:
                self.env.render()
            state = torch.FloatTensor(state).to(self.device)
            dist = self.actor(state)
            value = self.critic(state)

            action = dist.sample()
            state, reward, done, _ = self.env.step(action.cpu().numpy())
            fitness += reward
            log_prob = dist.log_prob(action).unsqueeze(0)

            episode['log_probs'].append(log_prob)
            episode['values'].append(value)
            episode['rewards'].append(reward)

            if done:
                break

        returns = self._compute_returns(episode['rewards'])

        log_probs = torch.cat(episode['log_probs'])
        values = torch.cat(episode['values'])
        returns = torch.FloatTensor(returns).to(self.device).detach()

        advantage = returns - values

        losses = {
            'actor': -(log_probs * advantage.detach()).mean(),
            'critic': advantage.pow(2).mean(),
        }

        for name, optimizer in self.optimizers.items():
            optimizer.zero_grad()
            losses[name].backward()
            optimizer.step()

        return fitness

    def _compute_returns(self, rewards):
        total_reward = 0
        returns = []
        for reward in reversed(rewards):
            total_reward = reward + self.gamma * total_reward
            returns.insert(0, total_reward)
        return returns

    def _call_reporters(self, stage: str, *args, **kwargs):
        for reporter in self.reporters:
            getattr(reporter, stage)(*args, **kwargs)