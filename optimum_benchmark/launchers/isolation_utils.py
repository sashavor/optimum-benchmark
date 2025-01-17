import os
import signal
import time
from contextlib import contextmanager
from logging import getLogger
from multiprocessing import Process
from typing import Set

from ..import_utils import is_amdsmi_available, is_psutil_available, is_pynvml_available
from ..logging_utils import setup_logging
from ..system_utils import is_nvidia_system, is_rocm_system

if is_psutil_available():
    import psutil

if is_pynvml_available():
    import pynvml

if is_amdsmi_available():
    import amdsmi  # type: ignore


LOGGER = getLogger("device-isolation")


def isolation_signal_handler(signum, frame):
    print(f"Process {os.getpid()} received an isolation signal. Exiting...")
    os._exit(signum)


signal.signal(signal.SIGUSR1, isolation_signal_handler)


def get_nvidia_devices_pids() -> Set[int]:
    if not is_pynvml_available():
        raise ValueError(
            "The library pynvml is required to get the pids running on NVIDIA GPUs, but is not installed. "
            "Please install the official and NVIDIA maintained PyNVML library through `pip install nvidia-ml-py`."
        )

    pynvml.nvmlInit()

    devices_pids = set()
    devices_ids = [int(device_id) for device_id in os.environ["CUDA_VISIBLE_DEVICES"].split(",")]

    for device_id in devices_ids:
        device_handle = pynvml.nvmlDeviceGetHandleByIndex(device_id)
        device_processes = pynvml.nvmlDeviceGetComputeRunningProcesses(device_handle)
        for device_process in device_processes:
            devices_pids.add(device_process.pid)

    pynvml.nvmlShutdown()

    return devices_pids


def get_amd_devices_pids() -> Set[int]:
    if not is_amdsmi_available():
        raise ValueError(
            "The library amdsmi is required to get the pids running on AMD GPUs, but is not installed. "
            "Please install the official and AMD maintained amdsmi library from https://github.com/ROCm/amdsmi."
        )

    amdsmi.amdsmi_init()

    devices_pids = set()
    devices_ids = [int(device_id) for device_id in os.environ["ROCR_VISIBLE_DEVICES"].split(",")]

    processor_handles = amdsmi.amdsmi_get_processor_handles()
    for device_id in devices_ids:
        processor_handle = processor_handles[device_id]
        try:
            # these functions fail a lot for no apparent reason
            processes_handles = amdsmi.amdsmi_get_gpu_process_list(processor_handle)
        except Exception:
            continue

        for process_handle in processes_handles:
            try:
                # these functions fail a lot for no apparent reason
                info = amdsmi.amdsmi_get_gpu_process_info(processor_handle, process_handle)
            except Exception:
                continue

            if info["memory_usage"]["vram_mem"] == 4096:
                # not sure why these processes are always present
                continue

            devices_pids.add(info["pid"])

    amdsmi.amdsmi_shut_down()

    return devices_pids


def get_pids_running_on_system_devices() -> Set[int]:
    """Returns the set of pids running on the system device(s)."""
    if is_nvidia_system():
        devices_pids = get_nvidia_devices_pids()
    elif is_rocm_system():
        devices_pids = get_amd_devices_pids()
    else:
        raise ValueError("get_pids_running_on_system_device is only supported on NVIDIA and AMD GPUs")

    return devices_pids


def assert_system_devices_isolation(isolated_pid: int) -> None:
    setup_logging("ERROR")
    isolation_pid = os.getpid()

    while psutil.pid_exists(isolated_pid):
        devices_pids = get_pids_running_on_system_devices()
        devices_pids = {pid for pid in devices_pids if psutil.pid_exists(pid)}
        isolated_children_pids = {child.pid for child in psutil.Process(isolated_pid).children(recursive=True)}
        isolation_children_pids = {child.pid for child in psutil.Process(isolation_pid).children(recursive=True)}
        permitted_pids = devices_pids | isolated_children_pids | isolation_children_pids
        non_permitted_pids = devices_pids - permitted_pids

        if len(non_permitted_pids) > 0:
            LOGGER.error(f"Found non-permitted process(es) running on system device(s): {non_permitted_pids}")

            for pid in permitted_pids:
                try:
                    LOGGER.error(f"Interrupting child process {pid} with an isolation signal...")
                    os.kill(pid, signal.SIGUSR1)
                except Exception as e:
                    LOGGER.error(f"Failed to interrupt child process {pid} with error {e}")

            LOGGER.error(f"Interrupting the isolated process {isolated_pid} with an isolation signal...")
            os.kill(isolated_pid, signal.SIGUSR1)
            LOGGER.error(f"Exiting isolation process {isolation_pid}...")
            exit(1)

        time.sleep(1)


@contextmanager
def device_isolation(enabled: bool, isolated_pid: int):
    if not enabled:
        yield
        return

    isolation_process = Process(
        target=assert_system_devices_isolation,
        kwargs={"isolated_pid": isolated_pid},
        daemon=True,
    )
    isolation_process.start()

    LOGGER.info(f"\t+ Launched device(s) isolation process {isolation_process.pid}.")

    yield

    LOGGER.info("\t+ Closing device(s) isolation process...")

    isolation_process.kill()
    isolation_process.join()
    isolation_process.close()
