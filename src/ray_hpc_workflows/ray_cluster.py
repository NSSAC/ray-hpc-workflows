"""Start a Ray cluster on Rivanna."""

import os
import json
import random
import atexit
import shlex
import socket
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent
from datetime import datetime
from dataclasses import dataclass
from collections import defaultdict

import ray

import platformdirs
from pydantic import BaseModel


from .utils import (
    Closeable,
    data_address,
    arbitrary_free_port,
    ignoring_sigint,
    find_ray,
    find_setup_script,
    terminate_gracefully,
)
from .slurm_job_manager import SlurmJob, SlurmJobManager

# from .prometheus_service import PrometheusService
# from .grafana_service import GrafanaService

WORKER_PORT_MIN = 20000
WORKER_PORT_MAX = 30000
NUM_PORTS_PER_WORKER = 50


@dataclass
class JobType:
    sbatch_args: list[str]
    num_cpus: int
    num_gpus: int
    resources: dict[str, int]


KNWON_JOB_TYPES: dict[str, JobType] = {}

# Rivanna
KNWON_JOB_TYPES["rivanna:bii"] = JobType(
    sbatch_args=[
        "--partition=bii",
        "--nodes=1 --ntasks-per-node=1 --cpus-per-task=40 --mem=0",
    ],
    num_cpus=40,
    num_gpus=0,
    resources=dict(node=1),
)

KNWON_JOB_TYPES["rivanna:bii-gpu"] = JobType(
    sbatch_args=[
        "--partition=bii-gpu",
        "--nodes=1 --ntasks-per-node=1 --cpus-per-task=40 --gres=gpu:4 --mem=0",
    ],
    num_cpus=40,
    num_gpus=4,
    resources=dict(node=1),
)

KNWON_JOB_TYPES["rivanna:bii-largemem:intel48"] = JobType(
    sbatch_args=[
        "--partition=bii-largemem",
        "--constraint=intel",
        "--nodes=1 --ntasks-per-node=1 --cpus-per-task=48 --mem=0 --exclusive",
    ],
    num_cpus=48,
    num_gpus=0,
    resources=dict(node=1),
)

KNWON_JOB_TYPES["rivanna:bii-largemem:intel40"] = JobType(
    sbatch_args=[
        "--partition=bii-largemem",
        "--constraint=intel",
        "--exclude=udc-aj36-[15-20]",
        "--nodes=1 --ntasks-per-node=1 --cpus-per-task=40 --mem=0 --exclusive",
    ],
    num_cpus=40,
    num_gpus=0,
    resources=dict(node=1),
)

KNWON_JOB_TYPES["rivanna:bii-largemem:amd"] = JobType(
    sbatch_args=[
        "--partition=bii-largemem",
        "--constraint=amd",
        "--nodes=1 --ntasks-per-node=1 --cpus-per-task=128 --mem=0 --exclusive",
    ],
    num_cpus=128,
    num_gpus=0,
    resources=dict(node=1),
)

KNWON_JOB_TYPES["rivanna:rivanna"] = JobType(
    sbatch_args=[
        "--partition=standard",
        "--constraint=rivanna",
        "--nodes=1 --ntasks-per-node=1 --cpus-per-task=40 --exclusive",
    ],
    num_cpus=40,
    num_gpus=0,
    resources=dict(node=1),
)

KNWON_JOB_TYPES["rivanna:afton"] = JobType(
    sbatch_args=[
        "--partition=standard",
        "--constraint=afton",
        "--nodes=1 --ntasks-per-node=1 --cpus-per-task=96 --exclusive",
    ],
    num_cpus=96,
    num_gpus=0,
    resources=dict(node=1),
)

# Anvil
KNWON_JOB_TYPES["anvil:wholenode"] = JobType(
    sbatch_args=[
        "--partition=wholenode",
        "--nodes=1 --ntasks-per-node=1 --cpus-per-task=128 --exclusive",
    ],
    num_cpus=128,
    num_gpus=0,
    resources=dict(node=1),
)


def define_job_type(name: str, job_type: JobType) -> None:
    if name in KNWON_JOB_TYPES:
        raise RuntimeError(f"Job type '{name}' already defined")
    KNWON_JOB_TYPES[name] = job_type


class WorkerInfo(BaseModel):
    slurm_job: SlurmJob
    worker_ports: set[int]


class RayCluster(Closeable):
    """Run a Ray cluster."""

    def __init__(
        self,
        account: str,
        runtime_h: int,
        work_dir: Path | str | None = None,
        sjm: SlurmJobManager | None = None,
        qos: str | None = None,
        reservation: str | None = None,
        log_to_driver: bool = False,
        ray_executable: Path | str | None = None,
        setup_script: Path | str | None = None,
        head_job_type: str | None = None,
        python_paths: list[str] | None = None,
        add_cwd_to_python_paths: bool = True,
        verbose: bool = False,
    ):
        """
        Initialize.

        Args:
            account: Slurm account name.
            runtime_h: Max runtime of jobs created using the cluster.
            work_dir: Path where log files and scripts will be created.
            sjm: Slurm job manager instance.
            qos: Slurm QOS to be used when creating jobs.
            reservation: Slurm reservation to use when creating jobs.
            log_to_driver: Passed to ray.init()
            setup_script: Setup script (bash) to be used for initializing jobs.
            head_job_type: If not None, defines the job type of the head node.
                If None the head node is not used for Ray tasks.
            python_paths: If not None, they are appended to PYTHONPATH
                enviroment variable of the workers.
            add_cwd_to_python_paths: If True, add current working directory to
                PYTHONPATH enviroment variable of the workers.
            verbose: Show verbose output.
        """
        user = os.environ["USER"]
        address = data_address(None)
        self.ray_executable = find_ray(ray_executable)
        self.setup_script = find_setup_script(setup_script)

        port = arbitrary_free_port(address)
        dashboard_port = arbitrary_free_port("")
        client_server_port = arbitrary_free_port(address)

        if work_dir is None:
            now = datetime.now().isoformat()
            work_dir = platformdirs.user_cache_path(appname=f"ray-work-dir-{now}")

        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        if sjm is None:
            sjm = SlurmJobManager(work_dir / "sjm", cancel_on_close=False)

        self.sjm = sjm
        self.account = account
        self.runtime_h = runtime_h
        self.work_dir = work_dir
        self.qos = qos
        self.reservation = reservation
        self.host = address
        self.port = port
        self.dashboard_host = socket.gethostname()
        self.dashboard_port = dashboard_port
        self.client_server_port = client_server_port

        self.plasma_dir = Path("/dev/shm") / user / f"ray_plasma_dir"
        self.plasma_dir.mkdir(parents=True, exist_ok=True)

        self.temp_dir = Path("/tmp") / user / "ray_temp_dir"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # prometheus_port = arbitrary_free_port("")
        # metrics_job_name = "Ray"
        # metrics_service_discovery_file = (
        #     self.temp_dir / "prom_metrics_service_discovery.json"
        # )
        # self.prometheus = PrometheusService(
        #     metrics_job_name=metrics_job_name,
        #     metrics_service_discovery_file=metrics_service_discovery_file,
        #     storage_path=work_dir / "prometheus",
        #     port=prometheus_port,
        #     verbose=verbose,
        # )

        # grafana_port = arbitrary_free_port("")
        # self.grafana = GrafanaService(
        #     prometheus_url=self.prometheus.web_url,
        #     provisioning_dir=work_dir / "grafana",
        #     port=grafana_port,
        #     verbose=verbose,
        # )

        cluster_python_path = []
        if "PYTHONPATH" in os.environ:
            cluster_python_path.extend(os.environ["PYTHONPATH"].split(":"))
        if python_paths is not None:
            cluster_python_path.extend(python_paths)
        if add_cwd_to_python_paths:
            cluster_python_path.append(str(Path.cwd()))
        cluster_python_path = ":".join(cluster_python_path)
        self.cluster_python_path = cluster_python_path

        my_pid = str(os.getpid())
        plasma_store_socket_name = self.temp_dir / f"plasma-store-socket-{my_pid}.sock"
        raylet_socket_name = self.temp_dir / f"raylet-socket-{my_pid}.sock"

        print("Starting Ray head service ...")
        if head_job_type is not None:
            self.head_job_type = KNWON_JOB_TYPES[head_job_type]
            resources_json = json.dumps(self.head_job_type.resources)

            cmd = f"""
            '{self.ray_executable!s}' start
                --head
                --node-ip-address={self.host}
                --node-name='head'
                --port={self.port}
                --include-dashboard=true
                --dashboard-host=''
                --dashboard-port={self.dashboard_port}
                --ray-client-server-port={self.client_server_port}
                --plasma-directory='{self.plasma_dir!s}'
                --temp-dir='{self.temp_dir!s}'
                --plasma-store-socket-name='{plasma_store_socket_name!s}'
                --raylet-socket-name='{raylet_socket_name!s}'
                --disable-usage-stats
                --verbose
                --log-style pretty
                --log-color false
                --num-cpus={self.head_job_type.num_cpus}
                --num-gpus={self.head_job_type.num_gpus}
                --resources='{resources_json}'
                --block
            """
        else:
            resources_json = json.dumps(dict(node=0))
            self.head_job_type = None

            cmd = f"""
            '{self.ray_executable!s}' start
                --head
                --node-ip-address={self.host}
                --node-name='head'
                --port={self.port}
                --include-dashboard=true
                --dashboard-host=''
                --dashboard-port={self.dashboard_port}
                --ray-client-server-port={self.client_server_port}
                --plasma-directory='{self.plasma_dir!s}'
                --temp-dir='{self.temp_dir!s}'
                --plasma-store-socket-name='{plasma_store_socket_name!s}'
                --raylet-socket-name='{raylet_socket_name!s}'
                --disable-usage-stats
                --verbose
                --log-style pretty
                --log-color false
                --num-cpus=0
                --num-gpus=0
                --resources='{resources_json}'
                --block
            """
        if verbose:
            cmd_str = dedent(cmd.strip())
            print(f"executing: {cmd_str}")
        cmd = shlex.split(cmd)

        env = dict(os.environ)
        # env["RAY_PROMETHEUS_HOST"] = self.prometheus.web_url
        # env["RAY_GRAFANA_HOST"] = self.grafana.dashboard_url
        env["RAY_scheduler_spread_threshold"] = "0.0"
        env["PYTHONPATH"] = self.cluster_python_path

        self._head_proc: subprocess.Popen | None
        with open(self.work_dir / "head.log", "at") as fobj:
            with ignoring_sigint():
                self._head_proc = subprocess.Popen(
                    cmd,
                    stdout=fobj,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                )

        self.next_worker_index: dict[str, int] = defaultdict(int)
        self.workers: dict[str, list[WorkerInfo]] = defaultdict(list)
        self.used_worker_ports = set()

        self.worker_address = f"{self.host}:{self.port}"
        self.client_address = f"ray://{self.host}:{self.client_server_port}"
        self.dashboard_url = f"http://{self.dashboard_host}:{self.dashboard_port}"

        ray.init(self.client_address, log_to_driver=log_to_driver)

        atexit.register(self.close)

        print(f"Ray dashboard URL: {self.dashboard_url}")

    def add_worker(self, name: str) -> None:
        """Add a worker of the given type."""
        worker_type = KNWON_JOB_TYPES[name]

        resources_json = json.dumps(worker_type.resources)

        worker_index = self.next_worker_index[name]
        self.next_worker_index[name] += 1
        worker_name = f"ray_worker.{name}.{worker_index}"

        worker_ports = set()
        while len(worker_ports) < NUM_PORTS_PER_WORKER:
            port = random.randint(WORKER_PORT_MIN, WORKER_PORT_MAX)
            if port not in self.used_worker_ports:
                worker_ports.add(port)
                self.used_worker_ports.add(port)
        worker_ports_str = sorted(str(p) for p in worker_ports)
        worker_ports_str = ",".join(worker_ports_str)

        worker_script = rf"""
        . '/etc/profile'
        . '{self.setup_script!s}'

        set -Eeuo pipefail
        set -x

        mkdir -p '{self.temp_dir!s}'
        mkdir -p '{self.plasma_dir!s}'

        exit_trap () {{
            echo "Saving logs."
            mkdir -p '{self.work_dir!s}/ray-logs/{worker_name}'
            rsync -av '{self.temp_dir!s}/session_latest/logs/' '{self.work_dir!s}/ray-logs/{worker_name}/'

            echo "Removing temp diectories."
            rm -rf '{self.temp_dir!s}'
            rm -rf '{self.plasma_dir!s}'
        }}

        trap exit_trap EXIT

        term_trap() {{
            echo "Stopping ray."

            '{self.ray_executable!s}' stop
        }}

        trap term_trap SIGTERM

        export RAY_scheduler_spread_threshold=0.0
        export PYTHONPATH='{self.cluster_python_path}'

        # Use ib0 ip for ray
        NODE_IP=$( ip addr show dev ib0 | awk '/inet/ {{print $2}}' | cut -d / -f 1 | head -n 1 )

        '{self.ray_executable!s}' start \
            --node-ip-address="$NODE_IP" \
            --worker-port-list="{worker_ports_str}" \
            --node-name='{worker_name}' \
            --address='{self.worker_address}' \
            --num-cpus={worker_type.num_cpus} \
            --num-gpus={worker_type.num_gpus} \
            --resources='{resources_json}' \
            --plasma-directory='{self.plasma_dir!s}' \
            --plasma-store-socket-name='{self.temp_dir!s}'/plasma-store-socket.sock \
            --raylet-socket-name='{self.temp_dir!s}'/raylet-socket.sock \
            --disable-usage-stats \
            --verbose \
            --log-style pretty \
            --log-color false \
            --block
        """
        worker_script = dedent(worker_script)

        sbatch_args = [
            f"--account {self.account}",
            f"--time {self.runtime_h}:00:00",
        ]
        sbatch_args.extend(worker_type.sbatch_args)

        if self.qos is not None:
            sbatch_args.append(f"--qos {self.qos}")
        if self.reservation is not None:
            sbatch_args.append(f"--reservation {self.reservation}")

        print(f"Starting worker {worker_name} ...")
        job = self.sjm.submit(
            worker_name, sbatch_args, worker_script % dict(NODE_NAME=worker_name)
        )

        worker_info = WorkerInfo(slurm_job=job, worker_ports=worker_ports)
        self.workers[name].append(worker_info)

    def num_cpus_requested(self) -> int:
        """Return the number of CPUs requested."""
        ret = 0

        for k, vs in self.workers.items():
            ret += KNWON_JOB_TYPES[k].num_cpus * len(vs)

        if self.head_job_type is not None:
            ret += self.head_job_type.num_cpus

        return ret

    def num_gpus_requested(self) -> int:
        """Return the number of GPUs requested."""
        ret = 0

        for k, vs in self.workers.items():
            ret += KNWON_JOB_TYPES[k].num_gpus * len(vs)

        if self.head_job_type is not None:
            ret += self.head_job_type.num_gpus

        return ret

    def resource_requested(self, name: str) -> float:
        """Return the amount of resource requested."""
        ret = 0.0

        for k, vs in self.workers.items():
            ret += KNWON_JOB_TYPES[k].resources.get(name, 0) * len(vs)

        if self.head_job_type is not None:
            ret += self.head_job_type.resources.get(name, 0)

        return ret

    def scale_workers(self, worker_type_name: str, num_workers: int):
        """Ensure given number of HPC workers are running."""
        # If number of workers is less that what we have, we scale up
        while len(self.workers[worker_type_name]) < num_workers:
            self.add_worker(worker_type_name)

        # If number of workers is more than what we need, we scale down
        while len(self.workers[worker_type_name]) > num_workers:
            worker_info = self.workers[worker_type_name].pop()

            print(f"Stopping worker {worker_info.slurm_job.name} ...")
            self.sjm.cancel(worker_info.slurm_job, term=True, full=True)

            # Release the ports
            self.used_worker_ports -= worker_info.worker_ports

    def close(self):
        """Shutdown the ray cluster and connections."""
        print("Closing ray cluster.")

        for worker_type_name in KNWON_JOB_TYPES:
            self.scale_workers(worker_type_name, 0)

        ray.shutdown()

        if self._head_proc is not None:
            terminate_gracefully(self._head_proc, proc_name="Ray head process")
            self._head_proc = None

        # self.prometheus.close()
        # self.grafana.close()

        if self.plasma_dir.exists():
            shutil.rmtree(self.plasma_dir)

        if self.temp_dir.exists():
            print("Saving head logs ...")
            log_dir = self.work_dir / "ray-logs" / "head"
            log_dir.mkdir(parents=True, exist_ok=True)

            cmd = [
                "rsync",
                "-av",
                f"{self.temp_dir!s}/session_latest/logs/",
                f"{log_dir!s}/",
            ]
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL)

            shutil.rmtree(self.temp_dir)
