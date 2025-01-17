import os
from abc import ABC
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any, Dict, Optional, TypeVar

from psutil import cpu_count

from ..system_utils import get_gpu_device_ids, is_nvidia_system, is_rocm_system
from ..task_utils import infer_library_from_model_name_or_path, infer_task_from_model_name_or_path

LOGGER = getLogger("backend")

# backends share the same hub kwargs
HUB_KWARGS = {
    "revision": "main",
    "force_download": False,
    "local_files_only": False,
    "trust_remote_code": False,
}


@dataclass
class BackendConfig(ABC):
    name: str
    version: str
    _target_: str

    model: Optional[str] = None

    task: Optional[str] = None
    library: Optional[str] = None

    device: Optional[str] = None
    device_ids: Optional[str] = None
    # yes we use a string here instead of a list
    # because it's easier to pass in a yaml or from cli
    # and it's consistent with GPU environment variables

    seed: int = 42
    inter_op_num_threads: Optional[int] = None
    intra_op_num_threads: Optional[int] = None

    hub_kwargs: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.model is None:
            raise ValueError("`model` must be specified.")

        if self.task is None:
            self.task = infer_task_from_model_name_or_path(self.model, self.hub_kwargs.get("revision", None))

        if self.library is None:
            self.library = infer_library_from_model_name_or_path(self.model, self.hub_kwargs.get("revision", None))

        if self.device is None:
            self.device = "cuda" if is_nvidia_system() or is_rocm_system() else "cpu"

        if ":" in self.device:
            LOGGER.warning("`device` was specified using PyTorch format (e.g. `cuda:0`) which is not recommended.")
            self.device = self.device.split(":")[0]
            self.device_ids = self.device.split(":")[1]
            LOGGER.warning(f"`device` and `device_ids` are now set to `{self.device}` and `{self.device_ids}`.")

        if self.device not in ["cuda", "cpu", "mps", "xla", "gpu"]:
            raise ValueError(f"`device` must be either `cuda`, `cpu`, `mps`, `xla` or `gpu`, but got {self.device}")

        if self.device == "cuda":
            if self.device_ids is None:
                LOGGER.warning("`device_ids` was not specified, using all available GPUs.")
                self.device_ids = get_gpu_device_ids()
                LOGGER.warning(f"`device_ids` is now set to `{self.device_ids}` based on system configuration.")

            if is_nvidia_system():
                os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
                os.environ["CUDA_VISIBLE_DEVICES"] = self.device_ids

            elif is_rocm_system():
                # https://rocm.docs.amd.com/en/latest/conceptual/gpu-isolation.html
                # ROCR_VISIBLE_DEVICES is better than HIP_VISIBLE_DEVICES/CUDA_VISIBLE_DEVICES
                os.environ["ROCR_VISIBLE_DEVICES"] = self.device_ids

        if self.library not in ["transformers", "diffusers", "timm"]:
            raise ValueError(f"`library` must be either `transformers`, `diffusers` or `timm`, but got {self.library}")

        if self.inter_op_num_threads is not None:
            if self.inter_op_num_threads == -1:
                self.inter_op_num_threads = cpu_count()

        if self.intra_op_num_threads is not None:
            if self.intra_op_num_threads == -1:
                self.intra_op_num_threads = cpu_count()

        self.hub_kwargs = {**HUB_KWARGS, **self.hub_kwargs}


BackendConfigT = TypeVar("BackendConfigT", bound=BackendConfig)
