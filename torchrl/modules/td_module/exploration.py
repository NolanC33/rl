# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Optional, Union

import numpy as np
import torch

from torchrl.data import CompositeSpec
from torchrl.data.utils import expand_as_right
from torchrl.envs.utils import exploration_mode
from torchrl.modules.td_module.common import (
    _forward_hook_safe_action,
    TDModule,
    TDModuleWrapper,
)

__all__ = ["EGreedyWrapper", "OrnsteinUhlenbeckProcessWrapper"]

from torchrl.data.tensordict.tensordict import _TensorDict


class EGreedyWrapper(TDModuleWrapper):
    """
    Epsilon-Greedy PO wrapper.

    Args:
        policy (TDModule): a deterministic policy.
        eps_init (scalar): initial epsilon value.
            default: 1.0
        eps_end (scalar): final epsilon value.
            default: 0.1
        annealing_num_steps (int): number of steps it will take for epsilon to reach the eps_end value

    Examples:
        >>> from torchrl.modules import EGreedyWrapper, Actor
        >>> from torchrl.data import NdBoundedTensorSpec, TensorDict
        >>> import torch
        >>> torch.manual_seed(0)
        >>> spec = NdBoundedTensorSpec(-1, 1, torch.Size([4]))
        >>> module = torch.nn.Linear(4, 4, bias=False)
        >>> policy = Actor(spec=spec, module=module)
        >>> explorative_policy = EGreedyWrapper(policy, eps_init=0.2)
        >>> td = TensorDict({"observation": torch.zeros(10, 4)}, batch_size=[10])
        >>> print(explorative_policy(td).get("action"))
        tensor([[ 0.0000,  0.0000,  0.0000,  0.0000],
                [ 0.0000,  0.0000,  0.0000,  0.0000],
                [-0.6986, -0.9366, -0.5837,  0.8596],
                [ 0.0000,  0.0000,  0.0000,  0.0000],
                [ 0.0000,  0.0000,  0.0000,  0.0000],
                [ 0.0000,  0.0000,  0.0000,  0.0000],
                [ 0.0000,  0.0000,  0.0000,  0.0000],
                [ 0.0000,  0.0000,  0.0000,  0.0000],
                [ 0.0000,  0.0000,  0.0000,  0.0000],
                [ 0.0000,  0.0000,  0.0000,  0.0000]], grad_fn=<CopyBackwards>)

    """

    def __init__(
        self,
        policy: TDModule,
        eps_init: float = 1.0,
        eps_end: float = 0.1,
        annealing_num_steps: int = 1000,
    ):
        super().__init__(policy)
        self.register_buffer("eps_init", torch.tensor([eps_init]))
        self.register_buffer("eps_end", torch.tensor([eps_end]))
        if self.eps_end > self.eps_init:
            raise RuntimeError("eps should decrease over time or be constant")
        self.annealing_num_steps = annealing_num_steps
        self.register_buffer("eps", torch.tensor([eps_init]))

    def step(self, frames: int = 1) -> None:
        """A step of epsilon decay.
        After self.annealing_num_steps, this function is a no-op.

        Args:
            frames (int): number of frames since last step.

        """
        for _ in range(frames):
            self.eps.data[0] = max(
                self.eps_end.item(),
                (
                    self.eps - (self.eps_init - self.eps_end) / self.annealing_num_steps
                ).item(),
            )

    def forward(self, tensordict: _TensorDict) -> _TensorDict:
        tensordict = self.td_module.forward(tensordict)
        if exploration_mode() == "random" or exploration_mode() is None:
            out = tensordict.get(self.td_module.out_keys[0])
            eps = self.eps.item()
            cond = (torch.rand(tensordict.shape, device=tensordict.device) < eps).to(
                out.dtype
            )
            cond = expand_as_right(cond, out)
            out = (
                cond * self.td_module.spec.rand(tensordict.shape).to(out.device)
                + (1 - cond) * out
            )
            tensordict.set(self.td_module.out_keys[0], out)
        return tensordict


class OrnsteinUhlenbeckProcessWrapper(TDModuleWrapper):
    """
    Ornstein-Uhlenbeck exploration policy wrapper as presented in "CONTINUOUS CONTROL WITH DEEP REINFORCEMENT LEARNING",
    https://arxiv.org/pdf/1509.02971.pdf.

    The OU exploration is to be used with continuous control policies and introduces a auto-correlated exploration
    noise. This enables a sort of 'structured' exploration.

        Noise equation:
            noise = prev_noise + theta * (mu - prev_noise) * dt + current_sigma * sqrt(dt) * W
        Sigma equation:
            current_sigma = (-(sigma - sigma_min) / (n_steps_annealing) * n_steps + sigma).clamp_min(sigma_min)

    To keep track of the steps and noise from sample to sample, an `"ou_prev_noise{id}"` and `"ou_steps{id}"` keys
    will be written in the input/output tensordict. It is expected that the tensordict will be zeroed at reset,
    indicating that a new trajectory is being collected. If not, and is the same tensordict is used for consecutive
    trajectories, the step count will keep on increasing across rollouts. Note that the collector classes take care of
    zeroing the tensordict at reset time.

    Args:
        policy (TDModule): a policy
        eps_init (scalar): initial epsilon value, determining the amount of noise to be added.
            default: 1.0
        eps_end (scalar): final epsilon value, determining the amount of noise to be added.
            default: 0.1
        annealing_num_steps (int): number of steps it will take for epsilon to reach the eps_end value.
            default: 1000
        theta (scalar): theta factor in the noise equation
            default: 0.15
        mu (scalar): OU average (mu in the noise equation).
            default: 0.0
        sigma (scalar): sigma value in the sigma equation.
            default: 0.2
        dt (scalar): dt in the noise equation.
            default: 0.01
        x0 (Tensor, ndarray, optional): initial value of the process.
            default: 0.0
        sigma_min (number, optional): sigma_min in the sigma equation.
            default: None
        n_steps_annealing (int): number of steps for the sigma annealing.
            default: 1000
        key (str): key of the action to be modified.
            default: "action"
        safe (bool): if True, actions that are out of bounds given the action specs will be projected in the space
            given the `TensorSpec.project` heuristic.
            default: True

    Examples:
        >>> from torchrl.modules import OrnsteinUhlenbeckProcessWrapper, Actor
        >>> from torchrl.data import NdBoundedTensorSpec, TensorDict
        >>> import torch
        >>> torch.manual_seed(0)
        >>> spec = NdBoundedTensorSpec(-1, 1, torch.Size([4]))
        >>> module = torch.nn.Linear(4, 4, bias=False)
        >>> policy = Actor(spec, module=module)
        >>> explorative_policy = OrnsteinUhlenbeckProcessWrapper(policy)
        >>> td = TensorDict({"observation": torch.zeros(10, 4)}, batch_size=[10])
        >>> print(explorative_policy(td))
    """

    def __init__(
        self,
        policy: TDModule,
        eps_init: float = 1.0,
        eps_end: float = 0.1,
        annealing_num_steps: int = 1000,
        theta: float = 0.15,
        mu: float = 0.0,
        sigma: float = 0.2,
        dt: float = 1e-2,
        x0: Optional[Union[torch.Tensor, np.ndarray]] = None,
        sigma_min: Optional[float] = None,
        n_steps_annealing: int = 1000,
        key: str = "action",
        safe: bool = True,
    ):
        super().__init__(policy)
        self.ou = _OrnsteinUhlenbeckProcess(
            theta=theta,
            mu=mu,
            sigma=sigma,
            dt=dt,
            x0=x0,
            sigma_min=sigma_min,
            n_steps_annealing=n_steps_annealing,
            key=key,
        )
        self.register_buffer("eps_init", torch.tensor([eps_init]))
        self.register_buffer("eps_end", torch.tensor([eps_end]))
        if self.eps_end > self.eps_init:
            raise ValueError(
                "eps should decrease over time or be constant, "
                f"got eps_init={eps_init} and eps_end={eps_end}"
            )
        self.annealing_num_steps = annealing_num_steps
        self.register_buffer("eps", torch.tensor([eps_init]))
        self.out_keys = list(self.td_module.out_keys) + self.ou.out_keys
        self._spec = CompositeSpec(
            **self.td_module._spec, **{key: None for key in self.ou.out_keys}
        )
        if len(set(self.out_keys)) != len(self.out_keys):
            raise RuntimeError(f"Got multiple identical output keys: {self.out_keys}")
        self.safe = safe
        if self.safe:
            self.register_forward_hook(_forward_hook_safe_action)

    def step(self, frames: int = 1) -> None:
        """Updates the eps noise factor.

        Args:
            frames (int): number of frames of the current batch (corresponding to the number of updates to be made).

        """
        for _ in range(frames):
            if self.annealing_num_steps > 0:
                self.eps.data[0] = max(
                    self.eps_end.item(),
                    (
                        self.eps
                        - (self.eps_init - self.eps_end) / self.annealing_num_steps
                    ).item(),
                )
            else:
                raise ValueError(
                    f"{self.__class__.__name__}.step() called when "
                    f"self.annealing_num_steps={self.annealing_num_steps}. Expected a strictly positive "
                    f"number of frames."
                )

    def forward(self, tensordict: _TensorDict) -> _TensorDict:
        tensordict = super().forward(tensordict)
        if exploration_mode() == "random" or exploration_mode() is None:
            tensordict = self.ou.add_sample(tensordict, self.eps.item())
        return tensordict


# Based on http://math.stackexchange.com/questions/1287634/implementing-ornstein-uhlenbeck-in-matlab
class _OrnsteinUhlenbeckProcess:
    def __init__(
        self,
        theta: float,
        mu: float = 0.0,
        sigma: float = 0.2,
        dt: float = 1e-2,
        x0: Optional[Union[torch.Tensor, np.ndarray]] = None,
        sigma_min: Optional[float] = None,
        n_steps_annealing: int = 1000,
        key: str = "action",
    ):
        self.mu = mu
        self.sigma = sigma

        if sigma_min is not None:
            self.m = -float(sigma - sigma_min) / float(n_steps_annealing)
            self.c = sigma
            self.sigma_min = sigma_min
        else:
            self.m = 0.0
            self.c = sigma
            self.sigma_min = sigma

        self.theta = theta
        self.mu = mu
        self.dt = dt
        self.x0 = x0 if x0 is not None else 0.0
        self.key = key
        self._noise_key = "_ou_prev_noise"
        self._steps_key = "_ou_steps"
        self.out_keys = [self.noise_key, self.steps_key]

    @property
    def noise_key(self):
        return self._noise_key  # + str(id(self))

    @property
    def steps_key(self):
        return self._steps_key  # + str(id(self))

    def _make_noise_pair(self, tensordict: _TensorDict) -> None:
        tensordict.set(
            self.noise_key,
            torch.zeros(tensordict.get(self.key).shape, device=tensordict.device),
        )
        tensordict.set(
            self.steps_key,
            torch.zeros(
                torch.Size([*tensordict.batch_size, 1]),
                dtype=torch.long,
                device=tensordict.device,
            ),
        )

    def add_sample(self, tensordict: _TensorDict, eps: float = 1.0) -> _TensorDict:

        if self.noise_key not in tensordict.keys():
            self._make_noise_pair(tensordict)

        prev_noise = tensordict.get(self.noise_key)
        prev_noise = prev_noise + self.x0

        n_steps = tensordict.get(self.steps_key)

        noise = (
            prev_noise
            + self.theta * (self.mu - prev_noise) * self.dt
            + self.current_sigma(n_steps)
            * np.sqrt(self.dt)
            * torch.randn_like(prev_noise)
        )
        tensordict.set_(self.noise_key, noise - self.x0)
        tensordict.set_(self.key, tensordict.get(self.key) + eps * noise)
        tensordict.set_(self.steps_key, n_steps + 1)
        return tensordict

    def current_sigma(self, n_steps: torch.Tensor) -> torch.Tensor:
        sigma = (self.m * n_steps + self.c).clamp_min(self.sigma_min)
        return sigma
