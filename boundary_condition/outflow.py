"""A library for the outflow boundary condition."""

import re

import numpy as np
from swirl_lm.utility import common_ops
from swirl_lm.utility import get_kernel_fn
from swirl_lm.utility import grid_parametrization
import tensorflow as tf
from google3.research.simulation.tensorflow.fluid.framework.tf1 import model_function
from google3.research.simulation.tensorflow.fluid.framework.tf1 import step_updater


def outflow_boundary_condition() -> step_updater.StatesUpdateFn:
  r"""Generates a update function for an outflow boundary condition.

  A forward Euler with upwinding scheme is used to solve the outflow boundary
  equation:
    ∂ϕ/∂t = -max(u) ∂ϕ/∂x.
  In discrete form:
    ϕⱼⁿ⁺¹ = (1 - Δt max(u)/Δx) ϕⱼⁿ + Δt max(u)/Δx) ϕⱼ₋₁ⁿ.
  The outflow velocity is rescaled so that the mass flux at the outlet is the
  same as the inlet. Note that this boundary condition can only be applied in
  the variable density solver where `rho` is in `states`.

  Returns:
    A function that updates the Dirichelt boundary condition for required
    variables in dimension 0 on face 1, i.e. all `additional_states` with key
    regular expression 'bc_(\w+)_0_1', with `\w+` being the variable name.
  """
  def get_boundary_update_fn(
      kernel_op: get_kernel_fn.ApplyKernelOp,
      replica_id: tf.Tensor,
      replicas: np.ndarray,
      states: model_function.StatesMap,
      additional_states: model_function.StatesMap,
      params: grid_parametrization.GridParametrization,
  ) -> model_function.StatesMap:
    """Computes the boundary condition for variables on the +x boundary."""
    del kernel_op, replica_id

    group_assignment = np.array(
        [np.reshape(replicas[i, :, :], -1) for i in range(replicas.shape[0])])

    u_max = common_ops.global_reduce(
        tf.concat([u_i[-params.halo_width - 1, :] for u_i in states['u']],
                  axis=0), tf.math.reduce_max, group_assignment)

    cfl = params.dt * u_max / params.dx
    coeff = 1.0 - cfl

    def mass_flux_x_face(face_index):
      """Computes the mass flux in x face at `face_index`."""
      return common_ops.global_reduce(
          tf.concat([
              rho_i[face_index, :] * u_i[face_index, :]
              for rho_i, u_i in zip(states['rho'], states['u'])
          ],
                    axis=0), tf.math.reduce_sum, group_assignment)

    mass_exit = mass_flux_x_face(-params.halo_width - 1)
    mass_inlet = mass_flux_x_face(params.halo_width)
    mass_correction = mass_inlet / mass_exit

    def update_boundary_values(var_name):
      """Update the boundary values for variable `var_name`."""
      bc_name = 'bc_{}_0_1'.format(var_name)
      correction_factor = mass_correction if var_name == 'u' else 1.0
      return [
          correction_factor * (coeff * u_j +
                               (1.0 - coeff) * u_j_1[-params.halo_width - 1, :])
          for u_j, u_j_1 in zip(additional_states[bc_name], states[var_name])
      ]

    return {
        key: update_boundary_values(re.split(r'bc_(\w+)_0_1', key)[1])
             if re.search(r'bc_(\w+)_0_1', key) else val
        for key, val in additional_states.items()
    }

  return get_boundary_update_fn
