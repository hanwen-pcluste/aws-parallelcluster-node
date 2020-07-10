# Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance with
# the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
from logging.config import fileConfig

import argparse
from botocore.config import Config
from configparser import ConfigParser

from common.schedulers.slurm_commands import get_nodes_info, set_nodes_down_and_power_save
from slurm_plugin.common import CONFIG_FILE_DIR, InstanceLauncher

log = logging.getLogger(__name__)


class SlurmResumeConfig:
    DEFAULTS = {
        "max_retry": 5,
        "max_batch_size": 100,
        "update_node_address": True,
        "proxy": "NONE",
        "logging_config": os.path.join(os.path.dirname(__file__), "logging", "parallelcluster_resume_logging.conf"),
    }

    def __init__(self, config_file_path):
        self._get_config(config_file_path)

    def __repr__(self):
        attrs = ", ".join(["{key}={value}".format(key=key, value=repr(value)) for key, value in self.__dict__.items()])
        return "{class_name}({attrs})".format(class_name=self.__class__.__name__, attrs=attrs)

    def _get_config(self, config_file_path):
        """Get resume program configuration."""
        log.info("Reading %s", config_file_path)

        config = ConfigParser()
        try:
            config.read_file(open(config_file_path, "r"))
        except IOError:
            log.error(f"Cannot read slurm cloud bursting scripts configuration file: {config_file_path}")
            raise

        self.region = config.get("slurm_resume", "region")
        self.cluster_name = config.get("slurm_resume", "cluster_name")
        self.max_batch_size = config.getint(
            "slurm_resume", "max_batch_size", fallback=self.DEFAULTS.get("max_batch_size")
        )
        self.update_node_address = config.getboolean(
            "slurm_resume", "update_node_address", fallback=self.DEFAULTS.get("update_node_address")
        )

        # Configure boto3 to retry 5 times by default
        self._boto3_config = {"retries": {"max_attempts": self.DEFAULTS.get("max_retry"), "mode": "standard"}}
        proxy = config.get("slurm_resume", "proxy", fallback=self.DEFAULTS.get("proxy"))
        if proxy != "NONE":
            self._boto3_config["proxies"] = {"https": proxy}
        self.boto3_config = Config(**self._boto3_config)
        self.logging_config = config.get("slurm_resume", "logging_config", fallback=self.DEFAULTS.get("logging_config"))

        log.info(self.__repr__())


def _handle_failed_nodes(node_list):
    """
    Fall back mechanism to handle failure when launching instances.

    When encountering a failure, want slurm to deallocate current nodes,
    and re-queue job to be run automatically by new nodes.
    To do this, set node to DOWN, so slurm will automatically re-queue job.
    Then set node to POWER_DOWN so suspend program will be run.
    Suspend program needs to properly clean up instances(if any) and set node back to IDLE in all cases.

    If this process is not done explicitly, slurm will wait until ResumeTimeout,
    then execute this process of setting nodes to DOWN then POWER_DOWN.
    To save time, should explicitly set nodes to DOWN then POWER_DOWN after encountering failure.
    """
    try:
        log.info("Following nodes marked as down and placed into power_down: %s", node_list)
        set_nodes_down_and_power_save(node_list, reason="Failure when resuming nodes")
    except Exception as e:
        log.error("Failed to place nodes %s into down/power_down with exception: %s", node_list, e)


def _add_instances(node_list, resume_config):
    """Launch EC2 instances for cloud nodes."""
    instance_launcher = InstanceLauncher(
        node_list,
        resume_config.region,
        resume_config.cluster_name,
        resume_config.boto3_config,
        resume_config.max_batch_size,
        resume_config.update_node_address,
    )
    instance_launcher.add_instances_for_nodes()
    return instance_launcher.failed_nodes


def _resume(arg_nodes, resume_config):
    """Launch new EC2 nodes according to nodes requested by slurm."""
    log.info("Launching EC2 instances for the following Slurm nodes: %s", arg_nodes)
    node_list = [node.name for node in get_nodes_info(arg_nodes)]
    log.info("Retrieved nodelist: %s", node_list)

    failed_nodes = _add_instances(node_list, resume_config)
    success_nodes = [node for node in node_list if node not in failed_nodes]
    log.info("Successfully launched nodes %s", success_nodes)
    if failed_nodes:
        log.error("Failed to launch following nodes, powering down: %s", failed_nodes)
        _handle_failed_nodes(failed_nodes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("nodes", help="Nodes to burst")
    args = parser.parse_args()
    try:
        resume_config = SlurmResumeConfig(os.path.join(CONFIG_FILE_DIR, "parallelcluster_slurm_resume.conf"))
        try:
            # Configure root logger
            fileConfig(resume_config.logging_config, disable_existing_loggers=False)
        except Exception as e:
            default_log_file = "/var/log/parallelcluster/slurm_resume.log"
            logging.basicConfig(
                filename=default_log_file,
                level=logging.INFO,
                format="%(asctime)s - [%(name)s:%(funcName)s] - %(levelname)s - %(message)s",
            )
            log.warning(
                "Unable to configure logging from %s, using default settings and writing to %s.\nException: %s",
                resume_config.logging_config,
                default_log_file,
                e,
            )
        _resume(args.nodes, resume_config)
    except Exception as e:
        log.exception("Encountered exception when requesting instances for %s: %s", args.nodes, e)
        _handle_failed_nodes(args.nodes)


if __name__ == "__main__":
    main()