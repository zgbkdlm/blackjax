# Copyright 2020- The Blackjax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Implements the (basic) user interfaces for Random Walk Rosenbluth-Metropolis-Hastings kernels.
Some interfaces are exposed here for convenience and for entry level users, who might be familiar
with simpler versions of the algorithms, but in all cases they are particular instantiations
of the Random Walk Rosenbluth-Metropolis-Hastings.

Let's note x_{t-1} to the previous position and x_t to the newly sampled one.

The variants offered are:

1. Proposal distribution as addition of random noice from previous position. This means
x_t = x_{t-1} + step. Function: `additive_step`

2. Independent proposal distribution: P(x_t) doesn't depend on x_{t_1}. Function: `irmh`

3. Proposal distribution using a symmetric function. That means P(x_t|x_{t-1}) = P(x_{t-1}|x_t).
 Function: `rmh` without proposal_logdensity_fn. See 'Metropolis Algorithm' in [1]

4. Asymmetric proposal distribution. Function: `rmh` with proposal_logdensity_fn.
 See 'Metropolis-Hastings' Algorithm in [1]

Reference: :cite:p:`gelman2014bayesian` Section 11.2

Examples
--------
    The simplest case is:

    .. code::

        random_walk = blackjax.additive_step_random_walk(logdensity_fn, blackjax.mcmc.random_walk.normal(sigma))
        state = random_walk.init(position)
        new_state, info = random_walk.step(rng_key, state)

    In all cases we can JIT-compile the step function for better performance

    .. code::

        step = jax.jit(random_walk.step)
        new_state, info = step(rng_key, state)

"""
from typing import Callable, NamedTuple, Optional, Tuple

import jax
import numpy as np
from jax import numpy as jnp

from blackjax.mcmc import proposal
from blackjax.types import Array, PRNGKey, PyTree
from blackjax.util import generate_gaussian_noise

__all__ = [
    "build_additive_step",
    "normal",
    "build_irmh",
    "build_rmh",
    "RWInfo",
    "RWState",
    "rmh_proposal",
    "build_rmh_transition_energy",
]


def normal(sigma: Array) -> Callable:
    """Normal Random Walk proposal.

    Propose a new position such that its distance to the current position is
    normally distributed. Suitable for continuous variables.

    Parameter
    ---------
    sigma:
        vector or matrix that contains the standard deviation of the centered
        normal distribution from which we draw the move proposals.

    """
    if jnp.ndim(sigma) > 2:
        raise ValueError("sigma must be a vector or a matrix.")

    def propose(rng_key: PRNGKey, position: PyTree) -> PyTree:
        return generate_gaussian_noise(rng_key, position, sigma=sigma)

    return propose


class RWState(NamedTuple):
    """State of the RW chain.

    position
        Current position of the chain.
    log_density
        Current value of the log-density

    """

    position: PyTree
    logdensity: float


class RWInfo(NamedTuple):
    """Additional information on the RW chain.

    This additional information can be used for debugging or computing
    diagnostics.

    acceptance_rate
        The acceptance probability of the transition, linked to the energy
        difference between the original and the proposed states.
    is_accepted
        Whether the proposed position was accepted or the original position
        was returned.
    proposal
        The state proposed by the proposal.

    """

    acceptance_rate: float
    is_accepted: bool
    proposal: RWState


def init(position: PyTree, logdensity_fn: Callable) -> RWState:
    """Create a chain state from a position.

    Parameters
    ----------
    position: PyTree
        The initial position of the chain
    logdensity_fn: Callable
        Log-probability density function of the distribution we wish to sample
        from.

    """
    return RWState(position, logdensity_fn(position))


def build_additive_step():
    """Build a Random Walk Rosenbluth-Metropolis-Hastings kernel

    Returns
    -------
    A kernel that takes a rng_key and a Pytree that contains the current state
    of the chain and that returns a new state of the chain along with
    information about the transition.
    """

    def kernel(
        rng_key: PRNGKey, state: RWState, logdensity_fn: Callable, random_step: Callable
    ) -> Tuple[RWState, RWInfo]:
        def proposal_generator(key_proposal, position):
            move_proposal = random_step(key_proposal, position)
            new_position = jax.tree_util.tree_map(jnp.add, position, move_proposal)
            return new_position

        inner_kernel = build_rmh()
        return inner_kernel(rng_key, state, logdensity_fn, proposal_generator)

    return kernel


def build_irmh() -> Callable:
    """
    Build an Independent Random Walk Rosenbluth-Metropolis-Hastings kernel. This implies
    that the proposal distribution does not depend on the particle being mutated :cite:p:`wang2022exact`.

    Returns
    -------
    A kernel that takes a rng_key and a Pytree that contains the current state
    of the chain and that returns a new state of the chain along with
    information about the transition.

    """

    def kernel(
        rng_key: PRNGKey,
        state: RWState,
        logdensity_fn: Callable,
        proposal_distribution: Callable,
    ) -> Tuple[RWState, RWInfo]:
        """
        Parameters
        ----------
        proposal_distribution
            A function that, given a PRNGKey, is able to produce a sample in the same
            domain of the target distribution.
        """

        def proposal_generator(rng_key: PRNGKey, position: PyTree):
            return proposal_distribution(rng_key)

        inner_kernel = build_rmh()
        return inner_kernel(rng_key, state, logdensity_fn, proposal_generator)

    return kernel


def build_rmh():
    """Build a Rosenbluth-Metropolis-Hastings kernel.
    Returns
    -------
    A kernel that takes a rng_key and a Pytree that contains the current state
    of the chain and that returns a new state of the chain along with
    information about the transition.

    """

    def kernel(
        rng_key: PRNGKey,
        state: RWState,
        logdensity_fn: Callable,
        transition_generator: Callable,
        proposal_logdensity_fn: Optional[Callable] = None,
    ) -> Tuple[RWState, RWInfo]:
        """Move the chain by one step using the Rosenbluth Metropolis Hastings
        algorithm.

        Parameters
        ----------
        rng_key:
           The pseudo-random number generator key used to generate random
           numbers.
        logdensity_fn:
            A function that returns the log-probability at a given position.
        transition_generator:
            A function that generates a candidate transition for the markov chain.
        proposal_logdensity_fn:
            For non-symmetric proposals, a function that returns the log-density
            to obtain a given proposal knowing the current state. If it is not
            provided we assume the proposal is symmetric.
        state:
            The current state of the chain.

        Returns
        -------
        The next state of the chain and additional information about the current
        step.

        """
        transition_energy = build_rmh_transition_energy(proposal_logdensity_fn)

        init_proposal, generate_proposal = proposal.asymmetric_proposal_generator(
            transition_energy, np.inf
        )

        proposal_generator = rmh_proposal(
            logdensity_fn, transition_generator, init_proposal, generate_proposal
        )
        sampled_proposal, do_accept, p_accept = proposal_generator(rng_key, state)
        new_state = sampled_proposal.state
        return new_state, RWInfo(p_accept, do_accept, new_state)

    return kernel


def build_rmh_transition_energy(proposal_logdensity_fn: Optional[Callable]) -> Callable:
    if proposal_logdensity_fn is None:

        def transition_energy(prev_state, new_state):
            return -new_state.logdensity

    else:

        def transition_energy(prev_state, new_state):
            return -new_state.logdensity - proposal_logdensity_fn(new_state, prev_state)

    return transition_energy


def rmh_proposal(
    logdensity_fn,
    transition_distribution,
    init_proposal,
    generate_proposal,
    sample_proposal: Callable = proposal.static_binomial_sampling,
) -> Callable:
    def build_trajectory(rng_key, initial_state: RWState) -> RWState:
        position, logdensity = initial_state
        new_position = transition_distribution(rng_key, position)
        return RWState(new_position, logdensity_fn(new_position))

    def generate(rng_key, state: RWState) -> Tuple[RWState, bool, float]:
        key_proposal, key_accept = jax.random.split(rng_key, 2)
        end_state = build_trajectory(key_proposal, state)
        new_proposal, _ = generate_proposal(state, end_state)
        previous_proposal = init_proposal(state)
        sampled_proposal, do_accept, p_accept = sample_proposal(
            key_accept, previous_proposal, new_proposal
        )
        return sampled_proposal, do_accept, p_accept

    return generate
