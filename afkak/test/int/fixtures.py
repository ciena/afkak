# -*- coding: utf-8 -*-
# Copyright 2014, 2015 Cyan, Inc.
# Copyright 2018, 2019, 2021 Ciena Corporation
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

import logging
import os
import os.path
import re
import shutil
import socket
import subprocess
import tempfile
import uuid
from datetime import datetime
from urllib.parse import urlparse

from afkak.test.int.service import ExternalService, SpawnedService
from afkak.test.testutil import random_string


def get_open_port():
    sock = socket.socket()
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class KafkaHarness(object):
    """
    `KafkaHarness` starts a Kafka cluster.

    :ivar str chroot:
        ZooKeeper chroot the Kafka brokers are configured to use.
    :ivar zk: `_ZookeeperFixture` instance
    :ivar brokers: `list` of `_KafakBroker` instances
    """

    @classmethod
    def start(cls, replicas, **kw):
        """
        Create and run a `KafkaHarness`.

        :param int replicas:
            Number of Kafka brokers to start. Must be an odd number.
        :param kw:
            Keyword arguments passed through to `_KafkaFixture`. The
            *replicas*, *zk_host*, *zk_port*, and *zk_chroot* are set
            automatically. *partitions* defaults to 8.
        """
        kw["zk_chroot"] = chroot = random_string(10)
        zk = _ZookeeperFixture.instance(chroot)
        kw["replicas"] = replicas
        kw.setdefault("partitions", 8)
        kw["zk_host"] = zk.host
        kw["zk_port"] = zk.port

        brokers = []
        for broker_id in range(replicas):
            brokers.append(_KafkaFixture.instance(broker_id, **kw))

        return cls(chroot, zk, brokers)

    def __init__(self, chroot, zk, brokers):
        """
        Private constructor.
        """
        self.chroot = chroot
        self.zk = zk
        self.brokers = brokers

    def halt(self):
        for broker in self.brokers:
            broker.close()
        self.zk.close()

    @property
    def bootstrap_hosts(self):
        """
        List of (host, port) tuples for the brokers.
        """
        return ["{}:{}".format(b.host, b.port) for b in self.brokers]


class _Fixture(object):
    kafka_version = os.environ.get("KAFKA_VERSION", "0.9.0.1")
    project_root = os.environ.get(
        "PROJECT_ROOT",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")),
    )
    kafka_root = os.environ.get("KAFKA_ROOT", os.path.join(project_root, "servers", kafka_version, "kafka-bin"))

    @classmethod
    def test_resource(cls, filename):
        return os.path.join(cls.project_root, "servers", cls.kafka_version, "resources", filename)

    @classmethod
    def kafka_run_class_args(cls, *args):
        result = [os.path.join(cls.kafka_root, "bin", "kafka-run-class.sh")]
        result.extend(args)
        return result

    @classmethod
    def kafka_run_class_env(cls):
        env = os.environ.copy()
        env["KAFKA_LOG4J_OPTS"] = "-Dlog4j.configuration=file:{}".format(cls.test_resource("log4j.properties"))
        return env

    @classmethod
    def render_template(cls, source_file, target_file, binding):
        with open(source_file, "r") as handle:
            template = handle.read()
        with open(target_file, "w") as handle:
            handle.write(template.format(**binding))


class _ZookeeperFixture(_Fixture):
    @classmethod
    def instance(cls, kafka_chroot):
        if "ZOOKEEPER_URI" in os.environ:  # pragma: no cover
            parse = urlparse(os.environ["ZOOKEEPER_URI"])
            (host, port) = (parse.hostname, parse.port)
            fixture = ExternalService(host, port)
        else:
            (host, port) = ("127.0.0.1", get_open_port())
            fixture = cls(host, port)

        fixture.open(kafka_chroot)
        return fixture

    def __init__(self, host, port):
        self.host = host
        self.port = port

        self.tmp_dir = None
        self.child = None
        self._log = logging.getLogger("fixtures.ZK")

    def __repr__(self):
        return "{}<{}:{}>".format(
            self.__class__.__name__,
            self.host,
            self.port,
        )

    def open(self, kafka_chroot):
        self.tmp_dir = tempfile.mkdtemp()
        properties_file = os.path.join(self.tmp_dir, "zookeeper.properties")

        properties = (
            "dataDir={tmp_dir}\n"
            "clientPort={port}\n"
            "clientPortAddress={host}\n"
            "maxClientCnxns=0\n"
            # Use reduced timeouts to speed up failovers.
            "tickTime=2000\n"
            "maxSessionTimeout=6000\n"
        ).format(
            tmp_dir=self.tmp_dir,
            host=self.host,
            port=self.port,
        )
        with open(properties_file, "w") as f:
            f.write(properties)
        self._log.info("Running local instance with config:\n%s", properties)

        # Configure Zookeeper child process
        args = self.kafka_run_class_args("org.apache.zookeeper.server.quorum.QuorumPeerMain", properties_file)
        env = self.kafka_run_class_env()
        start_re = re.compile(
            "binding to port /{host}:|Starting server.*ZooKeeperServerMain".format(host=re.escape(self.host)),
        )
        self._child = SpawnedService("zookeeper", self._log, args, env, start_re)
        self._child.start()

        self._log.debug("Creating ZooKeeper chroot node %r...", kafka_chroot)
        args = self.kafka_run_class_args(
            "org.apache.zookeeper.ZooKeeperMain",
            "-server",
            "%s:%d" % (self.host, self.port),
            "create",
            "/%s" % kafka_chroot,
            "afkak",
        )
        env = self.kafka_run_class_env()
        proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout, _stderr = proc.communicate()
        if proc.returncode != 0:  # pragma: no cover
            self._log.error(
                "Failed to create Zookeeper chroot node. Process exited %d and output:\n%s",
                proc.returncode,
                stdout.decode("utf-8", "replace"),
            )
            raise RuntimeError("Failed to create Zookeeper chroot node")
        self._log.debug("Done!")

    def close(self):
        self._log.debug("Stopping...")
        self._child.stop()
        self._child = None
        self._log.debug("Done!")
        shutil.rmtree(self.tmp_dir)


class _KafkaFixture(_Fixture):
    @classmethod
    def instance(
        cls,
        broker_id,
        zk_host,
        zk_port,
        zk_chroot,
        replicas,
        partitions,
        message_max_bytes=1000000,
    ):
        if zk_chroot is None:
            zk_chroot = "afkak_" + str(uuid.uuid4()).replace("-", "_")
        if "KAFKA_URI" in os.environ:  # pragma: no cover
            parse = urlparse(os.environ["KAFKA_URI"])
            (host, port) = (parse.hostname, parse.port)
            fixture = ExternalService(host, port)
        else:
            (host, port) = ("127.0.0.1", get_open_port())
            fixture = cls(
                host=host,
                port=port,
                broker_id=broker_id,
                zk_host=zk_host,
                zk_port=zk_port,
                zk_chroot=zk_chroot,
                replicas=replicas,
                partitions=partitions,
                message_max_bytes=message_max_bytes,
            )
            fixture.open()
        return fixture

    def __init__(
        self,
        host,
        port,
        broker_id,
        zk_host,
        zk_port,
        zk_chroot,
        replicas,
        partitions,
        message_max_bytes,
    ):
        self.host = host
        self.port = port
        self._log = logging.getLogger("fixtures.K{}".format(broker_id))

        self.broker_id = broker_id

        self.zk_host = zk_host
        self.zk_port = zk_port
        self.zk_chroot = zk_chroot

        self.replicas = replicas
        self.partitions = partitions
        assert replicas % 2 == 1, "replica count must be odd, not {}".format(replicas)
        self.min_insync_replicas = replicas // 2 + 1

        self.message_max_bytes = message_max_bytes

        self.tmp_dir = None
        self.child = None
        self.running = False
        self.restartable = False  # Only restartable after stop() call

    def __repr__(self):
        return "{}<node_id={} {}:{} {}>".format(
            self.__class__.__name__,
            self.broker_id,
            self.host,
            self.port,
            "running" if self.running else "stopped",
        )

    def open(self):
        if self.running:  # pragma: no cover
            self._log.debug("Instance already running")
            return

        self.tmp_dir = tempfile.mkdtemp()
        self._properties_file = os.path.join(self.tmp_dir, "kafka.properties")
        self._log.info("Running local instance...")
        self._log.info("  host       = %s", self.host)
        self._log.info("  port       = %s", self.port)
        self._log.info("  broker_id  = %s", self.broker_id)
        self._log.info("  zk_host    = %s", self.zk_host)
        self._log.info("  zk_port    = %s", self.zk_port)
        self._log.info("  zk_chroot  = %s", self.zk_chroot)
        self._log.info("  replicas   = %s", self.replicas)
        self._log.info("  partitions = %s", self.partitions)
        self._log.info("  msg_max_sz = %s", self.message_max_bytes)
        self._log.info("  tmp_dir    = %s", self.tmp_dir)

        # Create directories
        os.mkdir(os.path.join(self.tmp_dir, "logs"))
        os.mkdir(os.path.join(self.tmp_dir, "data"))

        # Generate configs
        template = self.test_resource("kafka.properties")
        self.render_template(template, self._properties_file, vars(self))

        self._log.debug("Starting...")
        self.child = self._make_child()
        self.child.start()
        self._log.debug("Done!")
        self.running = True

    def _make_child(self):
        """
        Configure Kafka child process
        """
        name = "kafka{}".format(self.broker_id)
        args = self.kafka_run_class_args("kafka.Kafka", self._properties_file)
        env = self.kafka_run_class_env()
        # Match a message like (0.9.0.1 and earlier):
        #
        #     [2018-07-17 18:06:00,915] INFO [Kafka Server 0], started (kafka.server.KafkaServer)
        #
        # Or like this (1.1.1):
        #
        #     [2018-09-27 17:23:46,818] INFO [KafkaServer id=0] started (kafka.server.KafkaServer)
        start_re = re.compile(
            (
                r"(" r"\[Kafka Server {broker_id}\], [Ss]tarted" r"|" r"\[KafkaServer id={broker_id}\] started" r")"
            ).format(broker_id=self.broker_id)
        )
        return SpawnedService(name, self._log, args, env, start_re)

    def close(self):
        if not self.running:  # pragma: no cover
            self._log.warning("Instance already stopped")
            return

        self._log.info("Stopping... %s at %s", self.tmp_dir, datetime.utcnow().isoformat())
        self.child.stop()
        self.child = None
        self._log.info("Done!")
        shutil.rmtree(self.tmp_dir)
        self.running = False

    def stop(self):
        """Stop/cleanup the child SpawnedService"""
        self.child.stop()
        self.child = None
        self._log.info("Child stopped.")
        self.running = False
        self.restartable = True

    def restart(self):
        """Start a new child SpawnedService with same settings and tmpdir"""
        if not self.restartable:  # pragma: no cover
            self._log.error(
                "*** Kafka [%s:%d]: Restart attempted when not stopped.",
                self.host,
                self.port,
            )
            return
        self.child = self._make_child()
        self._log.debug("Starting...")
        self.child.start()
        self._log.debug("Done!")
        self.running = True
        self.restartable = False
