# Copyright 2020 DeepMind Technologies Limited. All Rights Reserved.
# Copyright 2021 Daniel Reusche
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

# Based on https://github.com/deepmind/dm-haiku/blob/4ae60fd4fd2da3b2f8f9ad3ec6dfd893745b483b/examples/mnist_gan.ipynb

import functools
from typing import Any, NamedTuple

import haiku as hk
import jax
import optax
import jax.numpy as jnp

from haiku.nets import MLP
import jax.nn as nn

from chex import assert_shape, assert_equal_shape

# Uncomment to disable asserts
# chex.disable_asserts()

# Model
class BidSampler:
    def __init__(self, rng, bidders, items):
        self.bidders = bidders
        self.items = items

        self.key = rng

    def sample(self, num_samples):
        self.key, self.subkey = jax.random.split(self.key)

        sample = jnp.stack(
            [
                jax.random.uniform(self.subkey, (self.bidders, self.items))
                for _ in range(0, num_samples)
            ],
            axis=0,
        )
        return sample


# move b_i to the front of B
# B = [b_i, b_0, ..., b_i-1, b_i+1, ..., b_n]
def permute_along_bidders(B, i):
    head = B[:, 0:i]  # all bid profiles up to b_i
    tail = B[:, i + 1 :]  # all bid profiles after b_i
    b_i = B[:, i : i + 1]  # b_i, slice this way to preserve shape
    permuted = jnp.concatenate([b_i, head, tail], axis=1)

    assert_equal_shape([B, permuted])
    return permuted


class Auctioneer(hk.Module):
    """Auctioneer network."""

    def __init__(self, bidders, items, net_width, net_depth, name=None):
        super().__init__(name=name)
        self.bidders = bidders
        self.items = items
        self.net_width = net_width  # TODO: unify this between auct and misr?
        self.net_depth = net_depth

        self.layers = [self.bidders * self.items, self.net_width, self.net_depth]
        self.layers_alloc = [*self.layers, self.items]
        self.layers_pay = [*self.layers, 1]

        # Initialize MLPs
        self.alloc_prob = MLP(self.layers_alloc, activation=jnp.tanh)
        self.alloc_which = MLP(self.layers_alloc, activation=jnp.tanh)
        self.pay_mlp = MLP(self.layers_pay, activation=jnp.tanh)

    def __call__(self, vals):
        """Computes auctions, consisting of an allocation and a payment matrix."""

        # rows are bidders
        # columns are items

        # probability to allocate an item
        alloc = self.alloc_prob(jnp.ravel(vals))
        alloc = nn.sigmoid(alloc)
        assert_shape(alloc, (self.items,))

        # probability to allocate item j to bidder i
        L = jnp.stack(  # stack bidder vectors to get matrix
            # compute bidder vectors
            [
                self.alloc_which(jnp.ravel(permute_along_bidders(vals, i)))
                for i in range(self.bidders)
            ],
            axis=0,
        )

        # softmax to ensure feasibility (allocate every item at most once).
        L = nn.softmax(L, axis=0)
        assert_shape(L, (self.bidders, self.items))

        alloc = alloc * L
        assert_shape(alloc, (self.bidders, self.items))

        # fraction of utility each bidder pays to the mechanism
        pay = jnp.squeeze(
            jnp.stack(
                [
                    nn.sigmoid(self.pay_mlp(jnp.ravel(permute_along_bidders(vals, i))))
                    for i in range(self.bidders)
                ],
                axis=1,
            )
        )

        # Fix shape for single bidder case.
        if self.bidders == 1:
            pay = jnp.stack([pay])

        assert_shape(pay, (self.bidders,))

        # fractions of utilities * sum of allocations of all items
        # per bidder for a given bid profile
        pay = pay * jnp.sum(jnp.squeeze(vals) * alloc, axis=1)
        # NOTE: squeeze vals, since they are in a batch of size one

        assert_shape(pay, (self.bidders,))
        return alloc, pay


class Misreporter(hk.Module):
    """Misreporter network."""

    def __init__(self, bidders, items, net_width, net_depth, name=None):
        super().__init__(name=name)
        self.bidders = bidders
        self.items = items
        self.net_width = net_width
        self.net_depth = net_depth

        self.layers = [
            self.bidders * self.items,
            self.net_width,
            self.net_depth,
            self.items,
        ]

        # Initialize MLP
        self.misr_mlp = MLP(self.layers, activation=jnp.tanh)

    def __call__(self, vals):
        """Computes (approximately) optimal misreports for a given auction."""

        # TODO: JAXize more?
        m_ = []
        for i in range(self.bidders):
            misr = self.misr_mlp(jnp.ravel(permute_along_bidders(vals, i)))
            m_.append(misr)

        misreports = jnp.stack(m_)
        assert_shape(misreports, (self.bidders, self.items))

        # NOTE: sigmoid for [0,1] valuations, should be e.g. softplus for positive valuations
        misreports = nn.sigmoid(misreports)
        return misreports


def tree_shape(xs):
    return jax.tree_map(lambda x: x.shape, xs)


class TPALTuple(NamedTuple):
    auct: Any
    misr: Any


class TPALState(NamedTuple):
    params: TPALTuple
    opt_state: TPALTuple


class TPAL:
    """Two Player Auction Learner."""

    def __init__(self, bidders, items, net_width, net_depth):
        self.bidders = bidders
        self.items = items

        self.net_width = net_width
        self.net_depth = net_depth

        # Define the Haiku network transforms.
        # We don't use BatchNorm so we don't use `with_state`.
        self.auct_transform = hk.without_apply_rng(
            hk.transform(
                lambda *args: Auctioneer(
                    self.bidders, self.items, self.net_width, self.net_depth
                )(*args)
            )
        )

        self.misr_transform = hk.without_apply_rng(
            hk.transform(
                lambda *args: Misreporter(
                    self.bidders, self.items, self.net_width, self.net_depth
                )(*args)
            )
        )

        # Build the optimizers.
        self.optimizers = TPALTuple(
            # try 1e-2/1e-3, b1, b2 are defaults
            auct=optax.adamw(4e-4, b1=0.9, b2=0.999),
            misr=optax.adamw(4e-4, b1=0.9, b2=0.999),
        )

    @functools.partial(jax.jit, static_argnums=0)
    def initial_state(self, rng, vals):
        """Returns the initial parameters and optimize states."""
        # Get initial network parameters.
        rng, rng_auct, rng_misr = jax.random.split(rng, 3)

        params = TPALTuple(
            auct=self.auct_transform.init(rng_auct, vals),
            misr=self.misr_transform.init(rng_misr, vals),
        )

        print("Auctioneer: \n\n{}\n".format(tree_shape(params.auct)))
        print("Misreporter: \n\n{}\n".format(tree_shape(params.misr)))

        # Initialize the optimizers.
        opt_state = TPALTuple(
            auct=self.optimizers.auct.init(params.auct),
            misr=self.optimizers.misr.init(params.misr),
        )
        return TPALState(params=params, opt_state=opt_state)

    @functools.partial(jax.jit, static_argnums=0)
    def reinit_misr(self, rng, tpal_state, vals):
        """Reinitializes the misreporter."""

        # Get initial network parameters.
        rng, rng_misr = jax.random.split(rng)

        params = TPALTuple(
            auct=tpal_state.params.auct,
            misr=self.misr_transform.init(rng_misr, vals),
        )

        # Initialize the optimizers.
        opt_state = TPALTuple(
            auct=tpal_state.opt_state.auct,
            misr=self.optimizers.misr.init(params.misr),
        )
        return TPALState(params=params, opt_state=opt_state)

    # Calculate utilities for all players
    def utility(self, vals, alloc, pay):
        utilities = jnp.sum(jnp.squeeze(vals) * alloc, axis=1) - pay

        assert_equal_shape([utilities, pay])
        return utilities

    # check of utility[i] == utility_i
    def utility_i(self, vals, i, alloc, pay):
        return jnp.sum(alloc[i] * vals[i]) - pay[i]

    # Take misreports of bidder i while keeping the rest fixed
    def misr_bidder_i(self, vals, misrs, i):
        head = vals[:, 0:i]  # all bid profiles up to V_i
        tail = vals[:, i + 1 :]  # all bid profiles after V_i
        m_i = misrs[
            :, i : i + 1
        ]  # misreports of bidder i, slice this way to preserve shape
        V_minus_i = jnp.concatenate([head, m_i, tail], axis=1)

        assert_equal_shape([vals, V_minus_i])
        return V_minus_i

    def misr_utility(self, misreports, val_sample, auct_params):
        # TODO: JAXize more?
        misr_utils = []
        for i in range(0, self.bidders):
            misr_i = self.misr_bidder_i(val_sample, misreports, i)
            assert_shape(misr_i, (self.bidders, self.items))

            # Receive an auction for misr_i
            alloc_m, pay_m = self.auct_transform.apply(auct_params, misr_i)

            u_i = self.utility(val_sample, alloc_m, pay_m)
            u_i = u_i[i]
            assert_shape(u_i, ())

            misr_utils.append(u_i)

        u_misr = jnp.stack(misr_utils)
        return u_misr

    def auct_loss(self, auct_params, misr_params, val_sample):
        """Auctioneer loss."""

        # Receive an auction
        alloc, pay = self.auct_transform.apply(auct_params, val_sample)
        # Receive misreports
        misreports = self.misr_transform.apply(misr_params, val_sample)

        regret = nn.relu(
            self.misr_utility(misreports, val_sample, auct_params)
            - self.utility(val_sample, alloc, pay)
        )

        loss = -(jnp.sqrt(jnp.sum(pay)) - jnp.sqrt(jnp.sum(regret))) + jnp.sum(regret)

        return loss

    def misr_loss(self, misr_params, auct_params, val_sample):
        """Misreporter loss."""

        # Receive misreports
        misreports = self.misr_transform.apply(misr_params, val_sample)

        # Calculate utility for misreports
        u_misr = self.misr_utility(misreports, val_sample, auct_params)

        return -jnp.sum(u_misr)

    # Vectorize losses to use on batches
    def v_auct_loss(self, auct_params, misr_params, val_batch):
        v_al = jax.vmap(functools.partial(self.auct_loss, auct_params, misr_params))
        return jnp.mean(v_al(val_batch))

    def v_misr_loss(self, misr_params, auct_params, val_batch):
        v_ml = jax.vmap(functools.partial(self.misr_loss, misr_params, auct_params))
        return jnp.mean(v_ml(val_batch))

    @functools.partial(jax.jit, static_argnums=0)
    def update_auct(self, tpal_state, batch):
        """Performs a parameter update."""
        # Update the generator.
        auct_loss, auct_grads = jax.value_and_grad(self.v_auct_loss)(
            tpal_state.params.auct, tpal_state.params.misr, batch
        )
        auct_update, auct_opt_state = self.optimizers.auct.update(
            auct_grads, tpal_state.opt_state.auct, tpal_state.params.auct
        )
        auct_params = optax.apply_updates(tpal_state.params.auct, auct_update)

        params = TPALTuple(auct=auct_params, misr=tpal_state.params.misr)
        opt_state = TPALTuple(auct=auct_opt_state, misr=tpal_state.opt_state.misr)
        tpal_state = TPALState(params=params, opt_state=opt_state)
        log = {
            "auct_loss": auct_loss,
        }
        return tpal_state, log

    @functools.partial(jax.jit, static_argnums=0)
    def update_misr(self, tpal_state, batch):
        """Performs a parameter update."""
        # Update the misreporter.
        misr_loss, misr_grads = jax.value_and_grad(self.v_misr_loss)(
            tpal_state.params.misr, tpal_state.params.auct, batch
        )  # NOTE: Params of the network to be updated need to be the first arg.

        misr_update, misr_opt_state = self.optimizers.misr.update(
            misr_grads, tpal_state.opt_state.misr, tpal_state.params.misr
        )
        misr_params = optax.apply_updates(tpal_state.params.misr, misr_update)

        params = TPALTuple(misr=misr_params, auct=tpal_state.params.auct)
        opt_state = TPALTuple(misr=misr_opt_state, auct=tpal_state.opt_state.auct)
        tpal_state = TPALState(params=params, opt_state=opt_state)
        log = {
            "misr_loss": misr_loss,
        }
        return tpal_state, log


# Train a two player auction learner and return it with state.
def training(
    num_steps,
    misr_updates,
    misr_reinit_iv,
    misr_reinit_lim,
    batch_size,
    bidders,
    items,
    net_width,
    net_depth,
    # val_dist, TODO: add option to use different distributions
):
    # @title {vertical-output: true}

    log_every = num_steps // 100

    # Let's see what hardware we're working with. The training takes a few
    # minutes on a GPU, a bit longer on CPU.
    print(f"Number of devices: {jax.device_count()}")
    print("Device:", jax.devices()[0].device_kind)
    print("")

    # The model.
    tpal = TPAL(bidders, items, net_width, net_depth)

    # Top-level RNG.
    rng = jax.random.PRNGKey(1729)

    # Initialize the network and optimizer.
    rng, rng_sampler, rng_state_init, rng_misr_reinit = jax.random.split(rng, 4)

    # Initialize BidSampler
    sampler = BidSampler(rng_sampler, bidders, items)

    tpal_state = tpal.initial_state(rng_state_init, sampler.sample(1))

    steps = []
    auct_losses = []
    misr_losses = []

    for step in range(num_steps):
        # Sample valuations
        val_sample = sampler.sample(batch_size)

        if ((step % misr_reinit_iv) == 0) and (step <= misr_reinit_lim):
            tpal_state = tpal.reinit_misr(
                rng_misr_reinit, tpal_state, sampler.sample(1)
            )

        for _ in range(0, misr_updates):
            tpal_state, misr_log = tpal.update_misr(tpal_state, val_sample)

        tpal_state, auct_log = tpal.update_auct(tpal_state, val_sample)

        # Log the losses.
        if step % log_every == 0:
            # It's important to call `device_get` here so we don't take up device
            # memory by saving the losses.
            misr_log = jax.device_get(misr_log)
            auct_log = jax.device_get(auct_log)

            auct_loss = auct_log["auct_loss"]
            misr_loss = misr_log["misr_loss"]
            print(
                f"Step {step}: "
                f"auct_loss = {auct_loss:.3f}, misr_loss = {misr_loss:.3f}"
            )
            steps.append(step)
            auct_losses.append(auct_loss)
            misr_losses.append(misr_loss)

    return tpal, tpal_state


def test(tpal, tpal_state):
    rng = jax.random.PRNGKey(1337)
    sampler = BidSampler(rng, tpal.bidders, tpal.items)

    for _ in range(0, 10):
        val_sample = sampler.sample(1)

        # Receive an auction
        alloc, pay = tpal.auct_transform.apply(tpal_state.params.auct, val_sample)

        # Receive misreports
        misreports = tpal.misr_transform.apply(tpal_state.params.misr, val_sample)

        # TODO: put the squeeze in misr_utility?
        misr_util = tpal.misr_utility(
            misreports, jnp.squeeze(val_sample), tpal_state.params.auct
        )
        truth_util = tpal.utility(val_sample, alloc, pay)
        regret = misr_util - truth_util

        print("utility truthful: " + str(jnp.sum(truth_util)))
        print("utility misrep: " + str(jnp.sum(misr_util)))
        print("regret: " + str(jnp.sum(regret)))
        print("pay: " + str(jnp.sum(pay)))


# Short training, for tests
tpal, state = training(1000, 50, 500, 1000, 100, 10, 5, 200, 7)
# Medium length training
# tpal, state = training(50000, 50, 500, 1000, 100, 10, 5, 200, 7)
# Long training
# tpal, state = training(200000, 50, 1200, 50000, 100, 10, 5, 200, 7)
# Full training, large batches, high # of misreport updates
# tpal, state = training(200000, 100, 1200, 50000, 500, 10, 5, 200, 7)

print("Metrics of Two-Player Auction Learner")
test(tpal, state)
