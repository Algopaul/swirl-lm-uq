# Copyright 2023 The swirl_lm Authors.
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

# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A base data class for loading and accessing the RRTMGP lookup tables."""

import abc
import dataclasses
from typing import Any, Dict

import netCDF4 as nc
from swirl_lm.physics.radiation.optics import constants
from swirl_lm.physics.radiation.optics import data_loader_base as loader
from swirl_lm.utility import types
import tensorflow as tf


DRY_AIR_KEY = 'dry_air'


@dataclasses.dataclass(frozen=True)
class LookupGasOpticsBase(loader.DataLoaderBase, metaclass=abc.ABCMeta):
  """Abstract class for loading and accessing tables of optical properties."""
  # Volume mixing ratio (vmr) array index for H2O.
  idx_h2o: int
  # Volume mixing ratio (vmr) array index for O3.
  idx_o3: int
  # Number of gases used in the lookup table.
  n_gases: int
  # Number of frequency bands.
  n_bnd: int
  # Number of `g-points`.
  n_gpt: int
  # Number of atmospheric layers (=2, lower and upper atmospheres).
  n_atmos_layers: int
  # Number of reference temperatures for absorption coefficient lookup table.
  n_t_ref: int
  # Number of reference pressures for absorption lookup table.
  n_p_ref: int
  # Number of reference binary mixing fractions, for absorption coefficient
  # lookup table.
  n_mixing_fraction: int
  # Number of major absorbing gases.
  n_maj_absrb: int
  # Number of minor absorbing gases.
  n_minor_absrb: int
  # Number of minor absorbers in lower atmosphere.
  n_minor_absrb_lower: int
  # Number of minor absorbers in upper atmosphere.
  n_minor_absrb_upper: int
  # Number of minor contributors in the lower atmosphere.
  n_contrib_lower: int
  # Number of minor contributors in the upper atmosphere.
  n_contrib_upper: int
  # Reference pressure separating upper and lower atmosphere.
  p_ref_tropo: tf.Tensor
  # Reference temperature.
  t_ref_absrb: tf.Tensor
  # Reference pressure.
  p_ref_absrb: tf.Tensor
  # Minimum pressure supported by RRTMGP lookup tables.
  p_ref_min: tf.Tensor
  # Δt for reference temperature values (Δt is constant).
  dtemp: tf.Tensor
  # Δ for log of reference pressure values (Δlog(p) is constant).
  dln_p: tf.Tensor
  # Major absorbing species in each band `(n_bnd, n_atmos_layers, 2)`.
  key_species: tf.Tensor
  # Major absorption coefficient `(n_t_ref, n_p_ref, n_η, n_gpt)`.
  kmajor: tf.Tensor
  # Minor absorption coefficient in lower atmosphere `(n_t_ref, n_η,
  # n_contrib_lower)`.
  kminor_lower: tf.Tensor
  # Minor absorption coefficient in upper atmosphere `(n_t_ref, n_η,
  # n_contrib_upper)`.
  kminor_upper: tf.Tensor
  # Starting and ending `g-point` for each band `(n_bnd, 2)`.
  bnd_lims_gpt: tf.Tensor
  # Starting and ending wavenumber for each band `(n_bnd, 2)`.
  bnd_lims_wn: tf.Tensor
  # `g-point` limits for minor contributors in lower atmosphere `(
  # n_contrib_lower, 2)`.
  minor_lower_gpt_lims: tf.Tensor
  # `g-point` limits for minor contributors in upper atmosphere `(
  # n_contrib_upper, 2)`.
  minor_upper_gpt_lims: tf.Tensor
  # Minor gas (lower atmosphere) scales with density? `(n_minor_absrb_lower)`.
  minor_lower_scales_with_density: tf.Tensor
  # Minor gas (upper atmosphere) scales with density? `(n_minor_absrb_upper)`.
  minor_upper_scales_with_density: tf.Tensor
  # Minor gas (lower atmosphere) scales by compliment `(n_minor_absrb_lower)`.
  lower_scale_by_complement: tf.Tensor
  # Minor gas (upper atmosphere) scales by compliment `(n_minor_absrb_upper)`.
  upper_scale_by_complement: tf.Tensor
  # Reference pressures used by the lookup table `(n_p_ref)`.
  p_ref: tf.Tensor
  # Reference temperatures used by the lookup table `(n_t_ref)`.
  t_ref: tf.Tensor
  # Reference volume mixing ratios used by the lookup table `(n_t_ref, n_gases,
  # 2)`.
  vmr_ref: tf.Tensor
  # Mapping from gas name to index.
  idx_gases: Dict[str, int]

  @classmethod
  def _load_data(
      cls,
      ds: nc.Dataset,
      tables: types.VariableMap,
      dims: types.DimensionMap,
  ) -> Dict[str, Any]:
    """Preprocesses the RRTMGP gas optics data.

    Args:
      ds: The original netCDF Dataset containing the RRTMGP optics data.
      tables: The extracted data as a dictionary of `tf.Variable`s.
      dims: A dictionary containing dimension information for the tables.

    Returns:
      A dictionary containing dimension information and the preprocessed RRTMGP
      data as `tf.Variable`s.
    """
    p_ref = tables['press_ref']
    t_ref = tables['temp_ref']
    p_ref_min = tf.math.reduce_min(p_ref)
    dtemp = t_ref[1] - t_ref[0]
    dln_p = tf.math.log(p_ref[0]) - tf.math.log(p_ref[1])
    gas_names_ds = ds['gas_names'][:].data
    gas_names = []
    for gas_name in gas_names_ds:
      gas_names.append(
          ''.join([g_i.decode('utf-8') for g_i in gas_name]).strip()
      )
    # Prepend a dry air key to the list of names so that the 0 index is reserved
    # for dry air and all the other names follow a 1-based index system,
    # consistent with the RRTMGP species indices.
    gas_names.insert(0, constants.DRY_AIR_KEY)
    idx_gases = cls._create_index(gas_names)
    # Map all h2o related species to the same index.
    idx_h2o = idx_gases['h2o']
    # water vapor - foreign
    idx_gases['h2o_frgn'] = idx_h2o
    # water vapor - self-continua
    idx_gases['h2o_self'] = idx_h2o
    return dict(
        idx_h2o=idx_h2o,
        idx_o3=idx_gases['o3'],
        idx_gases=idx_gases,
        n_gases=dims['absorber'],
        n_bnd=dims['bnd'],
        n_gpt=dims['gpt'],
        n_atmos_layers=dims['atmos_layer'],
        n_t_ref=dims['temperature'],
        n_p_ref=dims['pressure'],
        n_maj_absrb=dims['absorber'],
        n_minor_absrb=dims['minor_absorber'],
        n_minor_absrb_lower=dims['minor_absorber_intervals_lower'],
        n_minor_absrb_upper=dims['minor_absorber_intervals_upper'],
        n_contrib_lower=dims['contributors_lower'],
        n_contrib_upper=dims['contributors_upper'],
        n_mixing_fraction=dims['mixing_fraction'],
        p_ref_tropo=tables['press_ref_trop'],
        t_ref_absrb=tables['absorption_coefficient_ref_T'],
        p_ref_absrb=tables['absorption_coefficient_ref_P'],
        key_species=tables['key_species'],
        kmajor=tables['kmajor'],
        kminor_lower=tables['kminor_lower'],
        kminor_upper=tables['kminor_upper'],
        bnd_lims_gpt=tables['bnd_limits_gpt'],
        bnd_lims_wn=tables['bnd_limits_wavenumber'],
        minor_lower_gpt_lims=tables['minor_limits_gpt_lower'],
        minor_upper_gpt_lims=tables['minor_limits_gpt_upper'],
        minor_lower_scales_with_density=tables[
            'minor_scales_with_density_lower'
        ],
        minor_upper_scales_with_density=tables[
            'minor_scales_with_density_upper'
        ],
        lower_scale_by_complement=tables['scale_by_complement_lower'],
        upper_scale_by_complement=tables['scale_by_complement_upper'],
        p_ref=p_ref,
        t_ref=t_ref,
        p_ref_min=p_ref_min,
        dtemp=dtemp,
        dln_p=dln_p,
        vmr_ref=tables['vmr_ref'],
    )
