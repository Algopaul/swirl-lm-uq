R"""A library for monitoring analytical quantities (go/ns-solver-monitor).

The monitor object holds a dictionary that manages the analytical quantities
from all modules in the Navier-Stokes solver.

The keys for the analytical quantities are specified in the config proto as
"helper_var_keys", which follows the following pattern:
  "MONITOR_[\w+]",
where "[\w+]" is the name of the analytical quantity.
"""

import enum
import functools
import re
from typing import Dict, Sequence, Text, Union

from absl import logging
import numpy as np
from swirl_lm.utility import monitor_pb2
from swirl_lm.utility import types
import tensorflow as tf

from google3.research.simulation.tensorflow.fluid.framework import analytics_util
from google3.research.simulation.tensorflow.fluid.models.incompressible_structured_mesh import incompressible_structured_mesh_config

_MONITOR_NAME_DELIMITER = '_'
_TF_DTYPE = types.TF_DTYPE

MONITOR_KEY_TEMPLATE = 'MONITOR_{module}_{statistic_type}_{metric_name}'
MonitorDataType = Dict[Text, tf.Tensor]
FlowFieldMap = types.FlowFieldMap


class StatisticType(enum.Enum):
  """The statistic type for analytics."""
  UNKNOWN = 'unknown'
  MOMENT = 'moment'

  # This type of monitor data is used to store point-wise values for the
  # entire field. The data buffer will be initialized as a 3D float32 tensor
  # with the shape of [nz, nx, ny], where `nx`, `ny`, `nz` are the lengths of
  # the local grid including halos. The transpose (such that nz is in the
  # leading dimension) is to make it easier for the update operations where
  # most quantities are in the format of a list of x-y slices with length `nz`.
  RAW = 'raw'

  # This type of monitor data is used to store scalar values for each
  # sub-iteration in a simulation step. The number of sub-iterations the buffer
  # can handle is determined by the `corrector_nit` field in the params used to
  # construct the Monitor object. Note that the type naming uses `-` to avoid
  # the delimiter `_` which is used to parse different segments of the name.
  SUBITER_SCALAR = 'subiter-scalar'


class Monitor(object):
  """A library for collecting and monitoring fluid simulation analytics."""

  _MONITOR_VAR_PREFIX = 'MONITOR'

  def __init__(
      self,
      params: incompressible_structured_mesh_config
      .IncompressibleNavierStokesParameters,
  ):
    """Initializes the monitor and creates containers for analytics."""
    self._params = params
    self._monitor_spec = params.monitor_spec

    # If time averaging is enabled, the analytics quantities are averaged over
    # time.
    self._time_averaging = False
    self._averaging_start_step = 0
    self._averaging_end_step = None
    if self._monitor_spec.HasField('time_averaging'):
      self._time_averaging = True
      if self._monitor_spec.time_averaging.HasField('start_time_seconds'):
        start_time_secs = self._monitor_spec.time_averaging.start_time_seconds
        self._averaging_start_step = int(start_time_secs // params.dt)
      if self._monitor_spec.time_averaging.HasField('end_time_seconds'):
        end_time_secs = self._monitor_spec.time_averaging.end_time_seconds
        self._averaging_end_step = int(end_time_secs // params.dt)

    # Boundary conditions.
    self._homogeneous_dims = params.periodic_dims

    # TODO(b/204126949): We should place the monitor variables on the TPU rather
    # than the CPU, by initializing them in a separate function rather than the
    # constructor. That function should get called by `init_fn` in the
    # Navier-Stokes solver.
    # The container that holds all analytical quantities.
    self._data = self.monitor_var_init()

    # A dict that maps each state name to all its analytics processors.
    self._processors = self._init_processors()

  def _initial_moment(self):
    """Initializes a tensor for the moment statistic."""
    if self._homogeneous_dims is None:
      return [tf.constant(0, shape=(1, 1), dtype=_TF_DTYPE)]

    # The moment tensors have dimension of 1 in the homogeneous directions.
    halos = self._params.halo_width
    nx = 1 if self._homogeneous_dims[0] else self._params.nx - 2 * halos
    ny = 1 if self._homogeneous_dims[1] else self._params.ny - 2 * halos
    nz = 1 if self._homogeneous_dims[2] else self._params.nz - 2 * halos
    return tf.zeros(shape=(nz, nx, ny), dtype=_TF_DTYPE)

  def _compute_moment(
      self,
      state_name: Text,
      spec: monitor_pb2.AnalyticsSpec,
      states: FlowFieldMap,
      replicas: np.ndarray,
  ):
    """Computes the moment statistic for a specified field."""
    order = spec.moment_statistic.order
    halos = [self._params.halo_width] * 3
    second_state = None
    if spec.moment_statistic.HasField('second_state'):
      second_state = states[spec.moment_statistic.second_state]
    moment = analytics_util.moments(
        states[state_name], [order],
        halos,
        self._params.periodic_dims,
        replicas,
        f2=second_state)[0]
    return tf.stack(moment)

  def _raw_state(
      self,
      state_name: Text,
      spec: monitor_pb2.AnalyticsSpec,
      states: FlowFieldMap,
      replicas: np.ndarray,
  ):
    """Stacks a list of 2D tensors containing a subgrid field."""
    del spec, replicas
    return tf.stack(states[state_name])

  def _initial_raw(self):
    """Initializes a tensor for the raw statistic."""

    # The shape matches the internal field shape where they are represented as
    # lists of x-y slices (of shape [nx, ny]) with length of `nz`, where `nx`,
    # `ny` and `nz` are local grid lengths including halos.
    return tf.zeros(
        shape=(self._params.nz, self._params.nx, self._params.ny),
        dtype=_TF_DTYPE)

  def _initial_subiter_scalar(self):
    """Initializes a tensor for the subiter scalar statistic."""
    return tf.zeros(shape=(self._params.corrector_nit), dtype=_TF_DTYPE)

  def _make_analytics_processor(self, state_name: Text,
                                spec: monitor_pb2.AnalyticsSpec):
    """Creates a function that computes analytics as specified by `spec`."""
    processor = None
    if spec.WhichOneof('spec') == 'moment_statistic':
      processor = functools.partial(self._compute_moment, state_name, spec)
    elif spec.WhichOneof('spec') == 'raw_state':
      processor = functools.partial(self._raw_state, state_name, spec)
    return processor

  def _init_processors(self):
    """Initializes statistics processors for all variables of a given module."""
    all_processors = {}
    for state_analytics in self._monitor_spec.state_analytics:
      state_processors = {}
      for analytics_spec in state_analytics.analytics:
        key = analytics_spec.key
        state_processors[key] = self._make_analytics_processor(
            state_analytics.state_name, analytics_spec)
      all_processors[state_analytics.state_name] = state_processors
    return all_processors

  def compute_analytics(
      self,
      states: FlowFieldMap,
      replicas: np.ndarray,
      step: tf.Tensor = None,
  ) -> MonitorDataType:  # pytype: disable=annotation-type-mismatch
    """Computes and stores analytics for the given calculation module.

    Args:
      states: A Dict mapping field names to their local subgrid representation.
      replicas: A numpy array that maps grid coordinates to replica id numbers.
      step: A tf.Tensor holding the current simulation step. This is necessary
        only if time averaging is required for the analytics being computed.

    Returns:
      A dict containing all the updated monitor variables for the given module.
    """

    def should_time_filter():
      """Checks that the step should be included in the time average."""
      if not self._time_averaging or step is None:
        return tf.constant(False)
      check_lower_bound = step > self._averaging_start_step
      check_upper_bound = (
          self._averaging_end_step is None or step <= self._averaging_end_step)
      return tf.math.logical_and(check_lower_bound, check_upper_bound)

    def apply_time_filter(
        statistic: Union[tf.Tensor, Sequence[tf.Tensor]],
        prev_stat: Union[tf.Tensor, Sequence[tf.Tensor]]) -> tf.Tensor:
      """Applies the time filter to the analytics value."""
      if step is None:
        return statistic
      valid_count = tf.cast(
          step - self._averaging_start_step + 1, dtype=_TF_DTYPE)
      statistic = tf.nest.map_structure(
          lambda prev_stat, statistic: prev_stat +  # pylint:disable=g-long-lambda
          (statistic - prev_stat) / valid_count,
          prev_stat,
          statistic)
      return statistic

    monitor_vars = {}
    for state in states:
      if state not in self._processors:
        continue
      processors = self._processors[state]
      for key, processor in processors.items():
        if processor is None:
          continue
        statistic_k = processor(states, replicas)
        prev_stat = states[key]
        # pylint: disable=cell-var-from-loop
        statistic = tf.cond(
            pred=should_time_filter(),
            true_fn=functools.partial(apply_time_filter, statistic_k,
                                      prev_stat),
            false_fn=lambda: statistic_k)
        monitor_vars[key] = statistic
        self.update(key, statistic)

    return monitor_vars

  def monitor_var_init_from_spec(self):
    """Initializes the requested analytics from `MonitosSpec`."""
    vars_dict = {}
    for state_analytics in self._monitor_spec.state_analytics:
      for analytics_spec in state_analytics.analytics:
        key = analytics_spec.key
        if analytics_spec.WhichOneof('spec') == 'moment_statistic':
          init_value = self._initial_moment()
        elif analytics_spec.WhichOneof('spec') == 'raw_state':
          init_value = self._initial_raw()
        else:
          raise ValueError('Unsupported analytics type: %s' %
                           analytics_spec.WhichOneof('spec'))
        vars_dict.update({key: init_value})
    return vars_dict

  def monitor_var_init(self):
    """Initializes the requested analytics as a dictionary of 0."""
    regex = re.compile(r'MONITOR_[\w+]')
    monitor_var_names = list(filter(regex.match, self._params.helper_var_keys))

    vars_dict = {}
    for varname in monitor_var_names:
      if self.statistic_type(varname) == StatisticType.MOMENT:
        vars_dict[varname] = self._initial_moment()
      elif self.statistic_type(varname) == StatisticType.RAW:
        vars_dict[varname] = self._initial_raw()
      elif self.statistic_type(varname) == StatisticType.SUBITER_SCALAR:
        vars_dict[varname] = self._initial_subiter_scalar()
      else:
        # Statistic is a scalar.
        vars_dict[varname] = tf.constant(0, dtype=_TF_DTYPE)
    logging.info('Initialization of monitor data: monitor vars: %s',
                 str(vars_dict))

    vars_dict_from_spec = self.monitor_var_init_from_spec()
    vars_dict.update(vars_dict_from_spec)
    return vars_dict

  def _is_monitor_variable(self, varname: Text) -> bool:
    """Checks if `varname` is a monitor variable."""
    return re.search(r'MONITOR_[\w+]', varname) is not None

  def check_key(self, varnames: Union[Text, Sequence[Text]]) -> bool:
    """Checks if any `varnames` is one of the requested analytical quantities.

    Args:
      varnames: The names of the analytics to be updated.

    Returns:
      `True` if any `varnames` is a valid analytical quantity requested.
    """
    keys = self._data.keys()

    def _is_key(name):
      return name in keys

    if isinstance(varnames, str):
      return _is_key(varnames)

    for varname in varnames:
      if _is_key(varname):
        return True

    return False

  def update(self, varname: Text, value: tf.Tensor) -> None:
    """Updates the analytics with `varname`.

    Args:
      varname: The name of the analytics to be updated.
      value: The value of the anlaytical quantity.

    Raises:
      ValueError: If `varname` is not a requested analytical quantity or shape
        doesn't match with cached value shape.
    """
    if not self.check_key(varname):
      raise ValueError('{} is not a valid analytical variable in this '
                       'simulation.'.format(varname))
    if value.shape != self._data[varname].shape:
      raise ValueError(('{} shape mismatch in this simulation: {} (new) != {} '
                        '(cached).').format(varname, value.shape,
                                            self._data[varname].shape))

    self._data.update({varname: value})

  def statistic_type(self, key: Text) -> StatisticType:
    """Extracts the statistic name from a monitor key.

    Args:
      key: The monitor key.

    Returns:
      StatisticType for the key.
    """
    statistic_str = key.split(_MONITOR_NAME_DELIMITER)[2]
    if statistic_str.lower() == StatisticType.MOMENT.value:
      return StatisticType.MOMENT
    elif statistic_str.lower() == StatisticType.RAW.value:
      return StatisticType.RAW
    elif statistic_str.lower() == StatisticType.SUBITER_SCALAR.value:
      return StatisticType.SUBITER_SCALAR
    else:
      return StatisticType.UNKNOWN

  @property
  def data(self):
    """A library of the analytical variables requested by the user."""
    return self._data