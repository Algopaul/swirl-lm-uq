# Copyright 2022 The swirl_lm Authors.
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

"""A library for the source functions in the total energy equation."""

import numpy as np
from swirl_lm.base import parameters as parameters_lib
from swirl_lm.equations import common
from swirl_lm.equations import utils as eq_utils
from swirl_lm.equations.source_function import scalar_generic
from swirl_lm.numerics import calculus
from swirl_lm.physics import constants
from swirl_lm.physics.atmosphere import cloud
from swirl_lm.physics.atmosphere import microphysics_kw1978
from swirl_lm.physics.thermodynamics import thermodynamics_manager
from swirl_lm.physics.thermodynamics import water
from swirl_lm.utility import get_kernel_fn
from swirl_lm.utility import types
import tensorflow as tf

TOTAL_ENERGY_VARNAME = 'e_t'


class TotalEnergy(scalar_generic.ScalarGeneric):
  """Defines functions for source terms in the total energy equation.

  This class defines terms that computes the rhs `f(q)` of the total energy
  update equation `d rho_e_t / dt = f(e_t)`, where `e_t` is the total energy.
  """

  def __init__(
      self,
      kernel_op: get_kernel_fn.ApplyKernelOp,
      params: parameters_lib.SwirlLMParameters,
      scalar_name: str,
      thermodynamics: thermodynamics_manager.ThermodynamicsManager,
  ):
    """Retrieves context information for the total energy source.

    Args:
      kernel_op: An object that holds and performs all finite difference
        operations.
      params: The global context of the simulation.
      scalar_name: The name of the scalar that the source terms in this class is
        defined for. Only 'e_t' is allowed here.
      thermodynamics: An object that holds the thermodynamics library.
    """
    super().__init__(kernel_op, params, scalar_name, thermodynamics)

    assert scalar_name == TOTAL_ENERGY_VARNAME, (
        f'TotalEnegy is for {TOTAL_ENERGY_VARNAME} only, but {scalar_name} is'
        ' provided.'
    )

    self._include_radiation = (
        self._scalar_params.HasField('total_energy') and
        self._scalar_params.total_energy.include_radiation and
        self._g_dim is not None)

    self._include_subsidence = (
        self._scalar_params.HasField('total_energy') and
        self._scalar_params.total_energy.include_subsidence and
        self._g_dim is not None)

    self._include_precipitation = (
        self._scalar_params.HasField('total_energy') and
        self._scalar_params.total_energy.include_precipitation and
        self._g_dim is not None)

    self._cloud = None
    self._microphysics = None
    if isinstance(self._thermodynamics.model, water.Water):
      self._cloud = cloud.Cloud(
          self._thermodynamics.model)
      self._microphysics = microphysics_kw1978.MicrophysicsKW1978(
          params, self._thermodynamics.model
      )
    else:
      raise ValueError(
          '`water` thermodynamics is required for the total energy equation, '
          f'but {self._thermodynamics.model} is used.'
      )

  def _get_thermodynamic_variables(
      self,
      phi: types.FlowFieldVal,
      states: types.FlowFieldMap,
      additional_states: types.FlowFieldMap,
  ) -> types.FlowFieldMap:
    """Computes thermodynamic variables required to evaluate terms in equation.

    Args:
      phi: The variable `scalar_name` at the present iteration.
      states: A dictionary that holds all flow field variables.
      additional_states: A dictionary that holds all helper variables.

    Returns:
      A dictionary of thermodynamic variables.
    """
    if not isinstance(self._thermodynamics.model, water.Water):
      raise ValueError(
          '`water` thermodynamics is required for the humidity equation, but'
          f' {self._thermodynamics.model} is used.'
      )

    zz = additional_states.get('zz', tf.nest.map_structure(tf.zeros_like, phi))

    # Compute the temperature.
    q_t = states['q_t']
    rho_thermal = states['rho_thermal']

    if 'T' in additional_states.keys():
      temperature = additional_states['T']
    else:
      e = self._thermodynamics.model.internal_energy_from_total_energy(
          phi,
          states[common.KEY_U],
          states[common.KEY_V],
          states[common.KEY_W],
          zz,
      )
      temperature = self._thermodynamics.model.saturation_adjustment(
          'e_int', e, rho_thermal, q_t, additional_states)

    # Compute the potential temperature.
    # TODO(b/271625754): Remove the dependencies on temperature in additional
    # states,
    # END GOOGLE-INTERNAL
    if 'theta' in additional_states:
      theta = additional_states['theta']
    else:
      buf = self._thermodynamics.model.potential_temperatures(
          temperature, q_t, rho_thermal, zz, additional_states)
      theta = buf['theta']

    # Compute the total enthalpy.
    h_t = self._thermodynamics.model.total_enthalpy(
        phi, rho_thermal, q_t, temperature
    )

    return {
        self._scalar_name: phi,
        'q_t': q_t,
        'zz': zz,
        'T': temperature,
        'theta': theta,
        'h_t': h_t,
    }

  def _get_scalar_for_convection(
      self,
      phi: types.FlowFieldVal,
      states: types.FlowFieldMap,
      additional_states: types.FlowFieldMap,
  ) -> types.FlowFieldVal:
    """Determines the scalar to be used to compute the convection term."""

    thermo_states = self._get_thermodynamic_variables(
        phi, states, additional_states
    )

    return thermo_states['h_t']

  def _get_scalar_for_diffusion(
      self,
      phi: types.FlowFieldVal,
      states: types.FlowFieldMap,
      additional_states: types.FlowFieldMap,
  ) -> types.FlowFieldVal:
    """Determines the scalar to be used to compute the diffusion term."""

    thermo_states = self._get_thermodynamic_variables(
        phi, states, additional_states
    )

    return thermo_states['h_t']

  def _get_wall_diffusive_flux_helper_variables(
      self,
      phi: types.FlowFieldVal,
      states: types.FlowFieldMap,
      additional_states: types.FlowFieldMap,
  ) -> types.FlowFieldMap:
    """Prepares the helper variables for the diffusive flux in wall models.

    Args:
      phi: The variable `scalar_name` at the present iteration.
      states: A dictionary that holds all flow field variables.
      additional_states: A dictionary that holds all helper variables.

    Returns:
      A dictionary of variables required by wall diffusive flux closure models.
    """
    helper_variables = {
        key: states[key] for key in common.KEYS_VELOCITY
    }

    thermo_states = self._get_thermodynamic_variables(
        phi, states, additional_states
    )

    helper_variables['theta'] = thermo_states['theta']

    return helper_variables

  def source_fn(
      self,
      replica_id: tf.Tensor,
      replicas: np.ndarray,
      phi: types.FlowFieldVal,
      states: types.FlowFieldMap,
      additional_states: types.FlowFieldMap,
  ) -> types.FlowFieldVal:
    """Computes the source term in the total energy transport equation.

    Args:
      replica_id: The index of the local core replica.
      replicas: A 3D array specifying the topology of the partition.
      phi: The variable `scalar_name` at the present iteration.
      states: A dictionary that holds all flow field variables.
      additional_states: A dictionary that holds all helper variables.

    Returns:
      The source term of total energy transport equation.
    """
    if not isinstance(self._thermodynamics.model, water.Water):
      raise ValueError(
          '`water` thermodynamics is required for the total energy equation, '
          f'but {self._thermodynamics.model} is used.'
      )

    rho = states[common.KEY_RHO]
    rho_thermal = states['rho_thermal']
    thermo_states = self._get_thermodynamic_variables(
        phi, states, additional_states
    )

    # Compute the shear stress tensor.
    if self._params.use_sgs:
      mu = tf.nest.map_structure(
          lambda nu_t, rho_i: (self._params.nu + nu_t) * rho_i,
          additional_states['nu_t'],
          rho,
      )
    else:
      mu = tf.nest.map_structure(lambda rho_i: self._params.nu * rho_i, rho)

    tau = eq_utils.shear_stress(
        self._kernel_op,
        mu,
        self._h[0],
        self._h[1],
        self._h[2],
        states[common.KEY_U],
        states[common.KEY_V],
        states[common.KEY_W],
    )

    # Compute the divergence of the combined source terms due to dilatation and
    # wind shear.
    def compute_rho_u_tau(
        tau_0j: types.FlowFieldVal,
        tau_1j: types.FlowFieldVal,
        tau_2j: types.FlowFieldVal,
    ) -> types.FlowFieldVal:
      """Computes 'rho u_i tau_ij'."""

      def u_dot_tau(
          u: tf.Tensor,
          v: tf.Tensor,
          w: tf.Tensor,
          tau_0j_l: tf.Tensor,
          tau_1j_l: tf.Tensor,
          tau_2j_l: tf.Tensor,
      ) -> tf.Tensor:
        """Computes the dot product of the velocity and stress tensor."""
        return u * tau_0j_l + v * tau_1j_l + w * tau_2j_l

      return tf.nest.map_structure(
          u_dot_tau,
          states[common.KEY_U],
          states[common.KEY_V],
          states[common.KEY_W],
          tau_0j,
          tau_1j,
          tau_2j,
      )

    rho_u_tau = [
        compute_rho_u_tau(tau['xx'], tau['yx'], tau['zx']),
        compute_rho_u_tau(tau['xy'], tau['yy'], tau['zy']),
        compute_rho_u_tau(tau['xz'], tau['yz'], tau['zz'])
    ]
    div_terms = rho_u_tau

    if self._include_radiation or self._include_precipitation:
      q_l, _ = self._thermodynamics.model.equilibrium_phase_partition(
          thermo_states['T'], states['rho_thermal'], thermo_states['q_t']
      )

    # Compute the source terms due to dilatation, wind shear, radiation,
    # subsidence velocity, and precipitation.
    if self._include_radiation:
      halos = [self._params.halo_width] * 3
      f_r = self._cloud.source_by_radiation(
          q_l,
          states['rho_thermal'],
          thermo_states['zz'],
          self._h[self._g_dim],
          self._g_dim,
          halos,
          replica_id,
          replicas,
      )
      div_terms[self._g_dim] = tf.nest.map_structure(
          lambda div_term_i, rho_i, f_r_i: div_term_i - rho_i * f_r_i,
          div_terms[self._g_dim],
          rho,
          f_r,
      )
    source = calculus.divergence(self._kernel_op, div_terms, self._h)

    if self._include_subsidence:
      src_subsidence = eq_utils.source_by_subsidence_velocity(
          self._kernel_op,
          rho,
          thermo_states['zz'],
          self._h[self._g_dim],
          thermo_states['h_t'],
          self._g_dim,
      )
      source = tf.nest.map_structure(tf.math.add, source, src_subsidence)

    if self._include_precipitation:
      e_v, e_l, _ = self._thermodynamics.model.internal_energy_components(
          thermo_states['T']
      )
      # Get conversion rates from cloud water to rain water (for liquid and
      # vapor phase).
      q_r = states['q_r']
      cloud_liquid_to_rain_water_rate = (
          self._microphysics.autoconversion_and_accretion(q_r, q_l)
      )
      q_c = self._thermodynamics.model.saturation_excess(
          thermo_states['T'], rho_thermal, thermo_states['q_t']
      )
      # Find q_v from the invariant q_t = q_c + q_v = q_l + q_i + q_v.
      q_v = tf.nest.map_structure(tf.math.subtract, thermo_states['q_t'], q_c)
      rain_water_evaporation_rate = (
          self._microphysics.evaporation(
              rho_thermal, thermo_states['T'], q_r, q_v, q_l, q_c))
      # Get potential energy.
      pe = tf.nest.map_structure(
          lambda zz_i: constants.G * zz_i, thermo_states['zz']
      )
      # Calculate source terms for vapor and liquid conversions, respectively.
      # Use that c_{q_v->q_l} = -c_{q_l->q_v}, i.e. minus the evaporation
      # rate.
      source_v = tf.nest.map_structure(
          lambda e, pe, rho, c_lv: (e + pe) * rho * (-c_lv), e_v, pe, rho,
          rain_water_evaporation_rate)
      source_l = tf.nest.map_structure(
          lambda e, pe, rho, c_lr: (e + pe) * rho * c_lr, e_l, pe, rho,
          cloud_liquid_to_rain_water_rate)
      source = tf.nest.map_structure(lambda s, sv, sl: s + sv + sl, source,
                                     source_v, source_l)

    return source
