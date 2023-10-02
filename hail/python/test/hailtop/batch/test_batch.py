import asyncio
import inspect
import secrets
import unittest

import pytest
import os
import subprocess as sp
import tempfile
from shlex import quote as shq
import uuid
import re
import orjson

import hailtop.fs as hfs
import hailtop.batch_client.client as bc
from hailtop import pip_version
from hailtop.batch import Batch, ServiceBackend, LocalBackend, ResourceGroup
from hailtop.batch.resource import JobResourceFile
from hailtop.batch.exceptions import BatchException
from hailtop.batch.globals import arg_max
from hailtop.utils import grouped, async_to_blocking
from hailtop.config import get_remote_tmpdir, configuration_of
from hailtop.batch.utils import concatenate
from hailtop.aiotools.router_fs import RouterAsyncFS
from hailtop.test_utils import skip_in_azure
from hailtop.httpx import ClientResponseError

from configparser import ConfigParser
from hailtop.config import get_user_config, user_config
from hailtop.config.variables import ConfigVariable
from hailtop.aiocloud.aiogoogle.client.storage_client import GoogleStorageAsyncFS
from _pytest.monkeypatch import MonkeyPatch


DOCKER_ROOT_IMAGE = os.environ.get('DOCKER_ROOT_IMAGE', 'ubuntu:22.04')
PYTHON_DILL_IMAGE = 'hailgenetics/python-dill:3.9-slim'
HAIL_GENETICS_HAIL_IMAGE = os.environ.get('HAIL_GENETICS_HAIL_IMAGE', f'hailgenetics/hail:{pip_version()}')


class LocalTests(unittest.TestCase):
    def batch(self, requester_pays_project=None):
        return Batch(backend=LocalBackend(),
                     requester_pays_project=requester_pays_project)

    def read(self, file):
        with open(file, 'r') as f:
            result = f.read().rstrip()
        return result

    def assert_same_file(self, file1, file2):
        assert self.read(file1).rstrip() == self.read(file2).rstrip()

    def test_read_input_and_write_output(self):
        with tempfile.NamedTemporaryFile('w') as input_file, \
                tempfile.NamedTemporaryFile('w') as output_file:
            input_file.write('abc')
            input_file.flush()

            b = self.batch()
            input = b.read_input(input_file.name)
            b.write_output(input, output_file.name)
            b.run()

            self.assert_same_file(input_file.name, output_file.name)

    def test_read_input_group(self):
        with tempfile.NamedTemporaryFile('w') as input_file1, \
                tempfile.NamedTemporaryFile('w') as input_file2, \
                tempfile.NamedTemporaryFile('w') as output_file1, \
                tempfile.NamedTemporaryFile('w') as output_file2:

            input_file1.write('abc')
            input_file2.write('123')
            input_file1.flush()
            input_file2.flush()

            b = self.batch()
            input = b.read_input_group(in1=input_file1.name,
                                       in2=input_file2.name)

            b.write_output(input.in1, output_file1.name)
            b.write_output(input.in2, output_file2.name)
            b.run()

            self.assert_same_file(input_file1.name, output_file1.name)
            self.assert_same_file(input_file2.name, output_file2.name)

    def test_write_resource_group(self):
        with tempfile.NamedTemporaryFile('w') as input_file1, \
                tempfile.NamedTemporaryFile('w') as input_file2, \
                tempfile.TemporaryDirectory() as output_dir:

            b = self.batch()
            input = b.read_input_group(in1=input_file1.name,
                                       in2=input_file2.name)

            b.write_output(input, output_dir + '/foo')
            b.run()

            self.assert_same_file(input_file1.name, output_dir + '/foo.in1')
            self.assert_same_file(input_file2.name, output_dir + '/foo.in2')

    def test_single_job(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            msg = 'hello world'

            b = self.batch()
            j = b.new_job()
            j.command(f'echo "{msg}" > {j.ofile}')
            b.write_output(j.ofile, output_file.name)
            b.run()

            assert self.read(output_file.name) == msg

    def test_single_job_with_shell(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            msg = 'hello world'

            b = self.batch()
            j = b.new_job(shell='/bin/bash')
            j.command(f'echo "{msg}" > {j.ofile}')

            b.write_output(j.ofile, output_file.name)
            b.run()

            assert self.read(output_file.name) == msg

    def test_single_job_with_nonsense_shell(self):
        b = self.batch()
        j = b.new_job(shell='/bin/ajdsfoijasidojf')
        j.image(DOCKER_ROOT_IMAGE)
        j.command(f'echo "hello"')
        self.assertRaises(Exception, b.run)

        b = self.batch()
        j = b.new_job(shell='/bin/nonexistent')
        j.command(f'echo "hello"')
        self.assertRaises(Exception, b.run)

    def test_single_job_with_intermediate_failure(self):
        b = self.batch()
        j = b.new_job()
        j.command(f'echoddd "hello"')
        j2 = b.new_job()
        j2.command(f'echo "world"')

        self.assertRaises(Exception, b.run)

    def test_single_job_w_input(self):
        with tempfile.NamedTemporaryFile('w') as input_file, \
                tempfile.NamedTemporaryFile('w') as output_file:
            msg = 'abc'
            input_file.write(msg)
            input_file.flush()

            b = self.batch()
            input = b.read_input(input_file.name)
            j = b.new_job()
            j.command(f'cat {input} > {j.ofile}')
            b.write_output(j.ofile, output_file.name)
            b.run()

            assert self.read(output_file.name) == msg

    def test_single_job_w_input_group(self):
        with tempfile.NamedTemporaryFile('w') as input_file1, \
                tempfile.NamedTemporaryFile('w') as input_file2, \
                tempfile.NamedTemporaryFile('w') as output_file:
            msg1 = 'abc'
            msg2 = '123'

            input_file1.write(msg1)
            input_file2.write(msg2)
            input_file1.flush()
            input_file2.flush()

            b = self.batch()
            input = b.read_input_group(in1=input_file1.name,
                                       in2=input_file2.name)
            j = b.new_job()
            j.command(f'cat {input.in1} {input.in2} > {j.ofile}')
            j.command(f'cat {input}.in1 {input}.in2')
            b.write_output(j.ofile, output_file.name)
            b.run()

            assert self.read(output_file.name) == msg1 + msg2

    def test_single_job_bad_command(self):
        b = self.batch()
        j = b.new_job()
        j.command("foo")  # this should fail!
        with self.assertRaises(sp.CalledProcessError):
            b.run()

    def test_declare_resource_group(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            msg = 'hello world'
            b = self.batch()
            j = b.new_job()
            j.declare_resource_group(ofile={'log': "{root}.txt"})
            assert isinstance(j.ofile, ResourceGroup)
            j.command(f'echo "{msg}" > {j.ofile.log}')
            b.write_output(j.ofile.log, output_file.name)
            b.run()

            assert self.read(output_file.name) == msg

    def test_resource_group_get_all_inputs(self):
        b = self.batch()
        input = b.read_input_group(fasta="foo",
                                   idx="bar")
        j = b.new_job()
        j.command(f"cat {input.fasta}")
        assert input.fasta in j._inputs
        assert input.idx in j._inputs

    def test_resource_group_get_all_mentioned(self):
        b = self.batch()
        j = b.new_job()
        j.declare_resource_group(foo={'bed': '{root}.bed', 'bim': '{root}.bim'})
        assert isinstance(j.foo, ResourceGroup)
        j.command(f"cat {j.foo.bed}")
        assert j.foo.bed in j._mentioned
        assert j.foo.bim not in j._mentioned

    def test_resource_group_get_all_mentioned_dependent_jobs(self):
        b = self.batch()
        j = b.new_job()
        j.declare_resource_group(foo={'bed': '{root}.bed', 'bim': '{root}.bim'})
        j.command(f"cat")
        j2 = b.new_job()
        j2.command(f"cat {j.foo}")

    def test_resource_group_get_all_outputs(self):
        b = self.batch()
        j1 = b.new_job()
        j1.declare_resource_group(foo={'bed': '{root}.bed', 'bim': '{root}.bim'})
        assert isinstance(j1.foo, ResourceGroup)
        j1.command(f"cat {j1.foo.bed}")
        j2 = b.new_job()
        j2.command(f"cat {j1.foo.bed}")

        for r in [j1.foo.bed, j1.foo.bim]:
            assert r in j1._internal_outputs
            assert r in j2._inputs

        assert j1.foo.bed in j1._mentioned
        assert j1.foo.bim not in j1._mentioned

        assert j1.foo.bed in j2._mentioned
        assert j1.foo.bim not in j2._mentioned

        assert j1.foo not in j1._mentioned

    def test_multiple_isolated_jobs(self):
        b = self.batch()

        output_files = []
        try:
            output_files = [tempfile.NamedTemporaryFile('w') for _ in range(5)]

            for i, ofile in enumerate(output_files):
                msg = f'hello world {i}'
                j = b.new_job()
                j.command(f'echo "{msg}" > {j.ofile}')
                b.write_output(j.ofile, ofile.name)
            b.run()

            for i, ofile in enumerate(output_files):
                msg = f'hello world {i}'
                assert self.read(ofile.name) == msg
        finally:
            [ofile.close() for ofile in output_files]

    def test_multiple_dependent_jobs(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            b = self.batch()
            j = b.new_job()
            j.command(f'echo "0" >> {j.ofile}')

            for i in range(1, 3):
                j2 = b.new_job()
                j2.command(f'echo "{i}" > {j2.tmp1}')
                j2.command(f'cat {j.ofile} {j2.tmp1} > {j2.ofile}')
                j = j2

            b.write_output(j.ofile, output_file.name)
            b.run()

            assert self.read(output_file.name) == "0\n1\n2"

    def test_select_jobs(self):
        b = self.batch()
        for i in range(3):
            b.new_job(name=f'foo{i}')
        self.assertTrue(len(b.select_jobs('foo')) == 3)

    def test_scatter_gather(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            b = self.batch()

            for i in range(3):
                j = b.new_job(name=f'foo{i}')
                j.command(f'echo "{i}" > {j.ofile}')

            merger = b.new_job()
            merger.command('cat {files} > {ofile}'.format(files=' '.join([j.ofile for j in sorted(b.select_jobs('foo'),
                                                                                                  key=lambda x: x.name,  # type: ignore
                                                                                                  reverse=True)]),
                                                          ofile=merger.ofile))

            b.write_output(merger.ofile, output_file.name)
            b.run()

            assert self.read(output_file.name) == '2\n1\n0'

    def test_add_extension_job_resource_file(self):
        b = self.batch()
        j = b.new_job()
        j.command(f'echo "hello" > {j.ofile}')
        assert isinstance(j.ofile, JobResourceFile)
        j.ofile.add_extension('.txt.bgz')
        assert j.ofile._value
        assert j.ofile._value.endswith('.txt.bgz')

    def test_add_extension_input_resource_file(self):
        input_file1 = '/tmp/data/example1.txt.bgz.foo'
        b = self.batch()
        in1 = b.read_input(input_file1)
        assert in1._value
        assert in1._value.endswith('.txt.bgz.foo')

    def test_file_name_space(self):
        with tempfile.NamedTemporaryFile('w', prefix="some file name with (foo) spaces") as input_file, \
                tempfile.NamedTemporaryFile('w', prefix="another file name with (foo) spaces") as output_file:

            input_file.write('abc')
            input_file.flush()

            b = self.batch()
            input = b.read_input(input_file.name)
            j = b.new_job()
            j.command(f'cat {input} > {j.ofile}')
            b.write_output(j.ofile, output_file.name)
            b.run()

            self.assert_same_file(input_file.name, output_file.name)

    def test_resource_group_mentioned(self):
        b = self.batch()
        j = b.new_job()
        j.declare_resource_group(foo={'bed': '{root}.bed'})
        assert isinstance(j.foo, ResourceGroup)
        j.command(f'echo "hello" > {j.foo}')

        t2 = b.new_job()
        t2.command(f'echo "hello" >> {j.foo.bed}')
        b.run()

    def test_envvar(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            b = self.batch()
            j = b.new_job()
            j.env('SOME_VARIABLE', '123abcdef')
            j.command(f'echo $SOME_VARIABLE > {j.ofile}')
            b.write_output(j.ofile, output_file.name)
            b.run()
            assert self.read(output_file.name) == '123abcdef'

    def test_concatenate(self):
        b = self.batch()
        files = []
        for _ in range(10):
            j = b.new_job()
            j.command(f'touch {j.ofile}')
            files.append(j.ofile)
        concatenate(b, files, branching_factor=2)
        assert len(b._jobs) == 10 + (5 + 3 + 2 + 1)
        b.run()

    def test_python_job(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            b = self.batch()
            head = b.new_job()
            head.command(f'echo "5" > {head.r5}')
            head.command(f'echo "3" > {head.r3}')

            def read(path):
                with open(path, 'r') as f:
                    i = f.read()
                return int(i)

            def multiply(x, y):
                return x * y

            def reformat(x, y):
                return {'x': x, 'y': y}

            middle = b.new_python_job()
            r3 = middle.call(read, head.r3)
            r5 = middle.call(read, head.r5)
            r_mult = middle.call(multiply, r3, r5)

            middle2 = b.new_python_job()
            r_mult = middle2.call(multiply, r_mult, 2)
            r_dict = middle2.call(reformat, r3, r5)

            tail = b.new_job()
            tail.command(f'cat {r3.as_str()} {r5.as_repr()} {r_mult.as_str()} {r_dict.as_json()} > {tail.ofile}')

            b.write_output(tail.ofile, output_file.name)
            b.run()
            assert self.read(output_file.name) == '3\n5\n30\n{\"x\": 3, \"y\": 5}'

    def test_backend_context_manager(self):
        with LocalBackend() as backend:
            b = Batch(backend=backend)
            b.run()

    def test_failed_jobs_dont_stop_non_dependent_jobs(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            b = self.batch()

            head = b.new_job()
            head.command(f'echo 1 > {head.ofile}')

            head2 = b.new_job()
            head2.command('false')

            tail = b.new_job()
            tail.command(f'cat {head.ofile} > {tail.ofile}')
            b.write_output(tail.ofile, output_file.name)
            self.assertRaises(Exception, b.run)
            assert self.read(output_file.name) == '1'

    def test_failed_jobs_stop_child_jobs(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            b = self.batch()

            head = b.new_job()
            head.command(f'echo 1 > {head.ofile}')
            head.command('false')

            head2 = b.new_job()
            head2.command(f'echo 2 > {head2.ofile}')

            tail = b.new_job()
            tail.command(f'cat {head.ofile} > {tail.ofile}')

            b.write_output(head2.ofile, output_file.name)
            b.write_output(tail.ofile, output_file.name)
            self.assertRaises(Exception, b.run)
            assert self.read(output_file.name) == '2'

    def test_failed_jobs_stop_grandchild_jobs(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            b = self.batch()

            head = b.new_job()
            head.command(f'echo 1 > {head.ofile}')
            head.command('false')

            head2 = b.new_job()
            head2.command(f'echo 2 > {head2.ofile}')

            tail = b.new_job()
            tail.command(f'cat {head.ofile} > {tail.ofile}')

            tail2 = b.new_job()
            tail2.depends_on(tail)
            tail2.command(f'echo foo > {tail2.ofile}')

            b.write_output(head2.ofile, output_file.name)
            b.write_output(tail2.ofile, output_file.name)
            self.assertRaises(Exception, b.run)
            assert self.read(output_file.name) == '2'

    def test_failed_jobs_dont_stop_always_run_jobs(self):
        with tempfile.NamedTemporaryFile('w') as output_file:
            b = self.batch()

            head = b.new_job()
            head.command(f'echo 1 > {head.ofile}')
            head.command('false')

            tail = b.new_job()
            tail.command(f'cat {head.ofile} > {tail.ofile}')
            tail.always_run()

            b.write_output(tail.ofile, output_file.name)
            self.assertRaises(Exception, b.run)
            assert self.read(output_file.name) == '1'


class ServiceTests(unittest.TestCase):
    def setUp(self):
        # https://stackoverflow.com/questions/42332030/pytest-monkeypatch-setattr-inside-of-test-class-method
        self.monkeypatch = MonkeyPatch()

        self.backend = ServiceBackend()

        remote_tmpdir = get_remote_tmpdir('hailtop_test_batch_service_tests')
        if not remote_tmpdir.endswith('/'):
            remote_tmpdir += '/'
        self.remote_tmpdir = remote_tmpdir + str(uuid.uuid4()) + '/'

        if remote_tmpdir.startswith('gs://'):
            match = re.fullmatch('gs://(?P<bucket_name>[^/]+).*', remote_tmpdir)
            assert match
            self.bucket = match.groupdict()['bucket_name']
        else:
            assert remote_tmpdir.startswith('hail-az://')
            if remote_tmpdir.startswith('hail-az://'):
                match = re.fullmatch('hail-az://(?P<storage_account>[^/]+)/(?P<container_name>[^/]+).*', remote_tmpdir)
                assert match
                storage_account, container_name = match.groups()
            else:
                assert remote_tmpdir.startswith('https://')
                match = re.fullmatch('https://(?P<storage_account>[^/]+).blob.core.windows.net/(?P<container_name>[^/]+).*', remote_tmpdir)
                assert match
                storage_account, container_name = match.groups()
            self.bucket = f'{storage_account}/{container_name}'

        self.cloud_input_dir = f'{self.remote_tmpdir}batch-tests/resources'

        token = uuid.uuid4()
        self.cloud_output_path = f'/batch-tests/{token}'
        self.cloud_output_dir = f'{self.remote_tmpdir}{self.cloud_output_path}'

        in_cluster_key_file = '/test-gsa-key/key.json'
        if not os.path.exists(in_cluster_key_file):
            in_cluster_key_file = None

        self.router_fs = RouterAsyncFS(gcs_kwargs={'gcs_requester_pays_configuration': 'hail-vdc', 'credentials_file': in_cluster_key_file},
                                       azure_kwargs={'credential_file': in_cluster_key_file})

        if not self.sync_exists(f'{self.remote_tmpdir}batch-tests/resources/hello.txt'):
            self.sync_write(f'{self.remote_tmpdir}batch-tests/resources/hello.txt', b'hello world')
        if not self.sync_exists(f'{self.remote_tmpdir}batch-tests/resources/hello spaces.txt'):
            self.sync_write(f'{self.remote_tmpdir}batch-tests/resources/hello spaces.txt', b'hello')
        if not self.sync_exists(f'{self.remote_tmpdir}batch-tests/resources/hello (foo) spaces.txt'):
            self.sync_write(f'{self.remote_tmpdir}batch-tests/resources/hello (foo) spaces.txt', b'hello')

    def tearDown(self):
        self.backend.close()

    def sync_exists(self, url):
        return async_to_blocking(self.router_fs.exists(url))

    def sync_write(self, url, data):
        return async_to_blocking(self.router_fs.write(url, data))

    def batch(self, **kwargs):
        name_of_test_method = inspect.stack()[1][3]
        return Batch(name=name_of_test_method,
                     backend=self.backend,
                     default_image=DOCKER_ROOT_IMAGE,
                     attributes={'foo': 'a', 'bar': 'b'},
                     **kwargs)

    def test_single_task_no_io(self):
        b = self.batch()
        j = b.new_job()
        j.command('echo hello')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_single_task_input(self):
        b = self.batch()
        input = b.read_input(f'{self.cloud_input_dir}/hello.txt')
        j = b.new_job()
        j.command(f'cat {input}')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_single_task_input_resource_group(self):
        b = self.batch()
        input = b.read_input_group(foo=f'{self.cloud_input_dir}/hello.txt')
        j = b.new_job()
        j.storage('10Gi')
        j.command(f'cat {input.foo}')
        j.command(f'cat {input}.foo')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_single_task_output(self):
        b = self.batch()
        j = b.new_job(attributes={'a': 'bar', 'b': 'foo'})
        j.command(f'echo hello > {j.ofile}')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_single_task_write_output(self):
        b = self.batch()
        j = b.new_job()
        j.command(f'echo hello > {j.ofile}')
        b.write_output(j.ofile, f'{self.cloud_output_dir}/test_single_task_output.txt')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_single_task_resource_group(self):
        b = self.batch()
        j = b.new_job()
        j.declare_resource_group(output={'foo': '{root}.foo'})
        assert isinstance(j.output, ResourceGroup)
        j.command(f'echo "hello" > {j.output.foo}')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_single_task_write_resource_group(self):
        b = self.batch()
        j = b.new_job()
        j.declare_resource_group(output={'foo': '{root}.foo'})
        assert isinstance(j.output, ResourceGroup)
        j.command(f'echo "hello" > {j.output.foo}')
        b.write_output(j.output, f'{self.cloud_output_dir}/test_single_task_write_resource_group')
        b.write_output(j.output.foo, f'{self.cloud_output_dir}/test_single_task_write_resource_group_file.txt')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_multiple_dependent_tasks(self):
        output_file = f'{self.cloud_output_dir}/test_multiple_dependent_tasks.txt'
        b = self.batch()
        j = b.new_job()
        j.command(f'echo "0" >> {j.ofile}')

        for i in range(1, 3):
            j2 = b.new_job()
            j2.command(f'echo "{i}" > {j2.tmp1}')
            j2.command(f'cat {j.ofile} {j2.tmp1} > {j2.ofile}')
            j = j2

        b.write_output(j.ofile, output_file)
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_specify_cpu(self):
        b = self.batch()
        j = b.new_job()
        j.cpu('0.5')
        j.command(f'echo "hello" > {j.ofile}')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_specify_memory(self):
        b = self.batch()
        j = b.new_job()
        j.memory('100M')
        j.command(f'echo "hello" > {j.ofile}')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_scatter_gather(self):
        b = self.batch()

        for i in range(3):
            j = b.new_job(name=f'foo{i}')
            j.command(f'echo "{i}" > {j.ofile}')

        merger = b.new_job()
        merger.command('cat {files} > {ofile}'.format(files=' '.join([j.ofile for j in sorted(b.select_jobs('foo'),
                                                                                              key=lambda x: x.name,  # type: ignore
                                                                                              reverse=True)]),
                                                      ofile=merger.ofile))

        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_file_name_space(self):
        b = self.batch()
        input = b.read_input(f'{self.cloud_input_dir}/hello (foo) spaces.txt')
        j = b.new_job()
        j.command(f'cat {input} > {j.ofile}')
        b.write_output(j.ofile, f'{self.cloud_output_dir}/hello (foo) spaces.txt')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_dry_run(self):
        b = self.batch()
        j = b.new_job()
        j.command(f'echo hello > {j.ofile}')
        b.write_output(j.ofile, f'{self.cloud_output_dir}/test_single_job_output.txt')
        b.run(dry_run=True)

    def test_verbose(self):
        b = self.batch()
        input = b.read_input(f'{self.cloud_input_dir}/hello.txt')
        j = b.new_job()
        j.command(f'cat {input}')
        b.write_output(input, f'{self.cloud_output_dir}/hello.txt')
        res = b.run(verbose=True)
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_cloudfuse_fails_with_read_write_mount_option(self):
        assert self.bucket
        path = f'/{self.bucket}{self.cloud_output_path}'

        b = self.batch()
        j = b.new_job()
        j.command(f'mkdir -p {path}; echo head > {path}/cloudfuse_test_1')
        j.cloudfuse(self.bucket, f'/{self.bucket}', read_only=False)

        try:
            b.run()
        except ClientResponseError as e:
            assert 'Only read-only cloudfuse requests are supported' in e.body, e.body
        else:
            assert False

    def test_cloudfuse_fails_with_io_mount_point(self):
        assert self.bucket
        path = f'/{self.bucket}{self.cloud_output_path}'

        b = self.batch()
        j = b.new_job()
        j.command(f'mkdir -p {path}; echo head > {path}/cloudfuse_test_1')
        j.cloudfuse(self.bucket, f'/io', read_only=True)

        try:
            b.run()
        except ClientResponseError as e:
            assert 'Cloudfuse requests with mount_path=/io are not supported' in e.body, e.body
        else:
            assert False

    def test_cloudfuse_read_only(self):
        assert self.bucket
        path = f'/{self.bucket}{self.cloud_output_path}'

        b = self.batch()
        j = b.new_job()
        j.command(f'mkdir -p {path}; echo head > {path}/cloudfuse_test_1')
        j.cloudfuse(self.bucket, f'/{self.bucket}', read_only=True)

        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'failure', str((res_status, res.debug_info()))

    def test_cloudfuse_implicit_dirs(self):
        assert self.bucket
        path = self.router_fs.parse_url(f'{self.remote_tmpdir}batch-tests/resources/hello.txt').path
        b = self.batch()
        j = b.new_job()
        j.command(f'cat /cloudfuse/{path}')
        j.cloudfuse(self.bucket, f'/cloudfuse', read_only=True)

        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_cloudfuse_empty_string_bucket_fails(self):
        assert self.bucket
        b = self.batch()
        j = b.new_job()
        with self.assertRaises(BatchException):
            j.cloudfuse('', '/empty_bucket')
        with self.assertRaises(BatchException):
            j.cloudfuse(self.bucket, '')

    def test_cloudfuse_submount_in_io_doesnt_rm_bucket(self):
        assert self.bucket
        b = self.batch()
        j = b.new_job()
        j.cloudfuse(self.bucket, '/io/cloudfuse')
        j.command(f'ls /io/cloudfuse/')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))
        assert self.sync_exists(f'{self.remote_tmpdir}batch-tests/resources/hello.txt')

    @skip_in_azure
    def test_fuse_requester_pays(self):
        b = self.batch(requester_pays_project='hail-vdc')
        j = b.new_job()
        j.cloudfuse('hail-test-requester-pays-fds32', '/fuse-bucket')
        j.command('cat /fuse-bucket/hello')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    @skip_in_azure
    def test_fuse_non_requester_pays_bucket_when_requester_pays_project_specified(self):
        assert self.bucket
        b = self.batch(requester_pays_project='hail-vdc')
        j = b.new_job()
        j.command(f'ls /fuse-bucket')
        j.cloudfuse(self.bucket, f'/fuse-bucket', read_only=True)

        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    @skip_in_azure
    def test_requester_pays(self):
        b = self.batch(requester_pays_project='hail-vdc')
        input = b.read_input('gs://hail-test-requester-pays-fds32/hello')
        j = b.new_job()
        j.command(f'cat {input}')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_benchmark_lookalike_workflow(self):
        b = self.batch()

        setup_jobs = []
        for i in range(10):
            j = b.new_job(f'setup_{i}').cpu(0.25)
            j.command(f'echo "foo" > {j.ofile}')
            setup_jobs.append(j)

        jobs = []
        for i in range(500):
            j = b.new_job(f'create_file_{i}').cpu(0.25)
            j.command(f'echo {setup_jobs[i % len(setup_jobs)].ofile} > {j.ofile}')
            j.command(f'echo "bar" >> {j.ofile}')
            jobs.append(j)

        combine = b.new_job(f'combine_output').cpu(0.25)
        for _ in grouped(arg_max(), jobs):
            combine.command(f'cat {" ".join(shq(j.ofile) for j in jobs)} >> {combine.ofile}')
        b.write_output(combine.ofile, f'{self.cloud_output_dir}/pipeline_benchmark_test.txt')
        # too slow
        # assert b.run().status()['state'] == 'success'

    def test_envvar(self):
        b = self.batch()
        j = b.new_job()
        j.env('SOME_VARIABLE', '123abcdef')
        j.command('[ $SOME_VARIABLE = "123abcdef" ]')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_single_job_with_shell(self):
        msg = 'hello world'
        b = self.batch()
        j = b.new_job(shell='/bin/sh')
        j.command(f'echo "{msg}"')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_single_job_with_nonsense_shell(self):
        b = self.batch()
        j = b.new_job(shell='/bin/ajdsfoijasidojf')
        j.command(f'echo "hello"')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'failure', str((res_status, res.debug_info()))

    def test_single_job_with_intermediate_failure(self):
        b = self.batch()
        j = b.new_job()
        j.command(f'echoddd "hello"')
        j2 = b.new_job()
        j2.command(f'echo "world"')

        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'failure', str((res_status, res.debug_info()))

    def test_input_directory(self):
        b = self.batch()
        input1 = b.read_input(self.cloud_input_dir)
        input2 = b.read_input(self.cloud_input_dir.rstrip('/') + '/')
        j = b.new_job()
        j.command(f'ls {input1}/hello.txt')
        j.command(f'ls {input2}/hello.txt')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_python_job(self):
        b = self.batch(default_python_image=PYTHON_DILL_IMAGE)
        head = b.new_job()
        head.command(f'echo "5" > {head.r5}')
        head.command(f'echo "3" > {head.r3}')

        def read(path):
            with open(path, 'r') as f:
                i = f.read()
            return int(i)

        def multiply(x, y):
            return x * y

        def reformat(x, y):
            return {'x': x, 'y': y}

        middle = b.new_python_job()
        r3 = middle.call(read, head.r3)
        r5 = middle.call(read, head.r5)
        r_mult = middle.call(multiply, r3, r5)

        middle2 = b.new_python_job()
        r_mult = middle2.call(multiply, r_mult, 2)
        r_dict = middle2.call(reformat, r3, r5)

        tail = b.new_job()
        tail.command(f'cat {r3.as_str()} {r5.as_repr()} {r_mult.as_str()} {r_dict.as_json()}')

        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))
        assert res.get_job_log(4)['main'] == "3\n5\n30\n{\"x\": 3, \"y\": 5}\n", str(res.debug_info())

    def test_python_job_w_resource_group_unpack_individually(self):
        b = self.batch(default_python_image=PYTHON_DILL_IMAGE)
        head = b.new_job()
        head.declare_resource_group(count={'r5': '{root}.r5',
                                           'r3': '{root}.r3'})
        assert isinstance(head.count, ResourceGroup)

        head.command(f'echo "5" > {head.count.r5}')
        head.command(f'echo "3" > {head.count.r3}')

        def read(path):
            with open(path, 'r') as f:
                r = int(f.read())
            return r

        def multiply(x, y):
            return x * y

        def reformat(x, y):
            return {'x': x, 'y': y}

        middle = b.new_python_job()
        r3 = middle.call(read, head.count.r3)
        r5 = middle.call(read, head.count.r5)
        r_mult = middle.call(multiply, r3, r5)

        middle2 = b.new_python_job()
        r_mult = middle2.call(multiply, r_mult, 2)
        r_dict = middle2.call(reformat, r3, r5)

        tail = b.new_job()
        tail.command(f'cat {r3.as_str()} {r5.as_repr()} {r_mult.as_str()} {r_dict.as_json()}')

        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))
        assert res.get_job_log(4)['main'] == "3\n5\n30\n{\"x\": 3, \"y\": 5}\n", str(res.debug_info())

    def test_python_job_can_write_to_resource_path(self):
        b = self.batch(default_python_image=PYTHON_DILL_IMAGE)

        def write(path):
            with open(path, 'w') as f:
                f.write('foo')
        head = b.new_python_job()
        head.call(write, head.ofile)

        tail = b.new_bash_job()
        tail.command(f'cat {head.ofile}')

        res = b.run()
        assert res
        assert tail._job_id
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))
        assert res.get_job_log(tail._job_id)['main'] == 'foo', str(res.debug_info())

    def test_python_job_w_resource_group_unpack_jointly(self):
        b = self.batch(default_python_image=PYTHON_DILL_IMAGE)
        head = b.new_job()
        head.declare_resource_group(count={'r5': '{root}.r5',
                                           'r3': '{root}.r3'})
        assert isinstance(head.count, ResourceGroup)

        head.command(f'echo "5" > {head.count.r5}')
        head.command(f'echo "3" > {head.count.r3}')

        def read_rg(root):
            with open(root['r3'], 'r') as f:
                r3 = int(f.read())
            with open(root['r5'], 'r') as f:
                r5 = int(f.read())
            return (r3, r5)

        def multiply(r):
            x, y = r
            return x * y

        middle = b.new_python_job()
        r = middle.call(read_rg, head.count)
        r_mult = middle.call(multiply, r)

        tail = b.new_job()
        tail.command(f'cat {r_mult.as_str()}')

        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))
        job_log_3 = res.get_job_log(3)
        assert job_log_3['main'] == "15\n", str((job_log_3, res.debug_info()))

    def test_python_job_w_non_zero_ec(self):
        b = self.batch(default_python_image=PYTHON_DILL_IMAGE)
        j = b.new_python_job()

        def error():
            raise Exception("this should fail")

        j.call(error)
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'failure', str((res_status, res.debug_info()))

    def test_python_job_incorrect_signature(self):
        b = self.batch(default_python_image=PYTHON_DILL_IMAGE)

        def foo(pos_arg1, pos_arg2, *, kwarg1, kwarg2=1):
            print(pos_arg1, pos_arg2, kwarg1, kwarg2)

        j = b.new_python_job()

        with pytest.raises(BatchException):
            j.call(foo)
        with pytest.raises(BatchException):
            j.call(foo, 1)
        with pytest.raises(BatchException):
            j.call(foo, 1, 2)
        with pytest.raises(BatchException):
            j.call(foo, 1, kwarg1=2)
        with pytest.raises(BatchException):
            j.call(foo, 1, 2, 3)
        with pytest.raises(BatchException):
            j.call(foo, 1, 2, kwarg1=3, kwarg2=4, kwarg3=5)

        j.call(foo, 1, 2, kwarg1=3)
        j.call(foo, 1, 2, kwarg1=3, kwarg2=4)

        # `print` doesn't have a signature but other builtins like `abs` do
        j.call(print, 5)
        j.call(abs, -1)
        with pytest.raises(BatchException):
            j.call(abs, -1, 5)

    def test_fail_fast(self):
        b = self.batch(cancel_after_n_failures=1)

        j1 = b.new_job()
        j1.command('false')

        j2 = b.new_job()
        j2.command('sleep 300')

        res = b.run()
        job_status = res.get_job(2).status()
        assert job_status['state'] == 'Cancelled', str((job_status, res.debug_info()))

    def test_service_backend_remote_tempdir_with_trailing_slash(self):
        backend = ServiceBackend(remote_tmpdir=f'{self.remote_tmpdir}/temporary-files/')
        b = Batch(backend=backend)
        j1 = b.new_job()
        j1.command(f'echo hello > {j1.ofile}')
        j2 = b.new_job()
        j2.command(f'cat {j1.ofile}')
        b.run()

    def test_service_backend_remote_tempdir_with_no_trailing_slash(self):
        backend = ServiceBackend(remote_tmpdir=f'{self.remote_tmpdir}/temporary-files')
        b = Batch(backend=backend)
        j1 = b.new_job()
        j1.command(f'echo hello > {j1.ofile}')
        j2 = b.new_job()
        j2.command(f'cat {j1.ofile}')
        b.run()

    def test_large_command(self):
        backend = ServiceBackend(remote_tmpdir=f'{self.remote_tmpdir}/temporary-files')
        b = Batch(backend=backend)
        j1 = b.new_job()
        long_str = secrets.token_urlsafe(15 * 1024)
        j1.command(f'echo "{long_str}"')
        b.run()

    def test_big_batch_which_uses_slow_path(self):
        backend = ServiceBackend(remote_tmpdir=f'{self.remote_tmpdir}/temporary-files')
        b = Batch(backend=backend)
        # 8 * 256 * 1024 = 2 MiB > 1 MiB max bunch size
        for _ in range(8):
            j1 = b.new_job()
            long_str = secrets.token_urlsafe(256 * 1024)
            j1.command(f'echo "{long_str}" > /dev/null')
        batch = b.run()
        assert not batch._submission_info.used_fast_path
        batch_status = batch.status()
        assert batch_status['state'] == 'success', str((batch.debug_info()))

    def test_query_on_batch_in_batch(self):
        sb = ServiceBackend(remote_tmpdir=f'{self.remote_tmpdir}/temporary-files')
        bb = Batch(backend=sb, default_python_image=HAIL_GENETICS_HAIL_IMAGE)

        tmp_ht_path = self.remote_tmpdir + '/' + secrets.token_urlsafe(32)

        def qob_in_batch():
            import hail as hl
            hl.utils.range_table(10).write(tmp_ht_path, overwrite=True)

        j = bb.new_python_job()
        j.env('HAIL_QUERY_BACKEND', 'batch')
        j.env('HAIL_BATCH_BILLING_PROJECT', configuration_of(ConfigVariable.BATCH_BILLING_PROJECT, None, ''))
        j.env('HAIL_BATCH_REMOTE_TMPDIR', self.remote_tmpdir)
        j.call(qob_in_batch)

        bb.run()

    def test_basic_async_fun(self):
        backend = ServiceBackend(remote_tmpdir=f'{self.remote_tmpdir}/temporary-files')
        b = Batch(backend=backend)

        j = b.new_python_job()
        j.call(asyncio.sleep, 1)

        batch = b.run()
        batch_status = batch.status()
        assert batch_status['state'] == 'success', str((batch.debug_info()))

    def test_async_fun_returns_value(self):
        backend = ServiceBackend(remote_tmpdir=f'{self.remote_tmpdir}/temporary-files')
        b = Batch(backend=backend)

        async def foo(i, j):
            await asyncio.sleep(1)
            return i * j

        j = b.new_python_job()
        result = j.call(foo, 2, 3)

        j = b.new_job()
        j.command(f'cat {result.as_str()}')

        batch = b.run()
        batch_status = batch.status()
        assert batch_status['state'] == 'success', str((batch_status, batch.debug_info()))
        job_log_2 = batch.get_job_log(2)
        assert job_log_2['main'] == "6\n", str((job_log_2, batch.debug_info()))

    def test_specify_job_region(self):
        b = self.batch(cancel_after_n_failures=1)
        j = b.new_job('region')
        possible_regions = self.backend.supported_regions()
        j.regions(possible_regions)
        j.command('true')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_always_copy_output(self):
        output_path = f'{self.cloud_output_dir}/test_always_copy_output.txt'

        b = self.batch()
        j = b.new_job()
        j.always_copy_output()
        j.command(f'echo "hello" > {j.ofile} && false')

        b.write_output(j.ofile, output_path)
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'failure', str((res_status, res.debug_info()))

        b2 = self.batch()
        input = b2.read_input(output_path)
        file_exists_j = b2.new_job()
        file_exists_j.command(f'cat {input}')

        res = b2.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))
        assert res.get_job_log(1)['main'] == "hello\n", str(res.debug_info())

    def test_no_copy_output_on_failure(self):
        output_path = f'{self.cloud_output_dir}/test_no_copy_output.txt'

        b = self.batch()
        j = b.new_job()
        j.command(f'echo "hello" > {j.ofile} && false')

        b.write_output(j.ofile, output_path)
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'failure', str((res_status, res.debug_info()))

        b2 = self.batch()
        input = b2.read_input(output_path)
        file_exists_j = b2.new_job()
        file_exists_j.command(f'cat {input}')

        res = b2.run()
        res_status = res.status()
        assert res_status['state'] == 'failure', str((res_status, res.debug_info()))

    def test_update_batch(self):
        b = self.batch()
        j = b.new_job()
        j.command('true')
        res = b.run()

        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

        j2 = b.new_job()
        j2.command('true')
        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_update_batch_with_dependencies(self):
        b = self.batch()
        j1 = b.new_job()
        j1.command('true')
        j2 = b.new_job()
        j2.command('false')
        res = b.run()

        res_status = res.status()
        assert res_status['state'] == 'failure', str((res_status, res.debug_info()))

        j3 = b.new_job()
        j3.command('true')
        j3.depends_on(j1)

        j4 = b.new_job()
        j4.command('true')
        j4.depends_on(j2)

        res = b.run()
        res_status = res.status()
        assert res_status['state'] == 'failure', str((res_status, res.debug_info()))

        assert res.get_job(3).status()['state'] == 'Success', str((res_status, res.debug_info()))
        assert res.get_job(4).status()['state'] == 'Cancelled', str((res_status, res.debug_info()))

    def test_update_batch_with_python_job_dependencies(self):
        b = self.batch()

        async def foo(i, j):
            await asyncio.sleep(1)
            return i * j

        j1 = b.new_python_job()
        j1.call(foo, 2, 3)

        batch = b.run()
        batch_status = batch.status()
        assert batch_status['state'] == 'success', str((batch_status, batch.debug_info()))

        j2 = b.new_python_job()
        j2.call(foo, 2, 3)

        batch = b.run()
        batch_status = batch.status()
        assert batch_status['state'] == 'success', str((batch_status, batch.debug_info()))

        j3 = b.new_python_job()
        j3.depends_on(j2)
        j3.call(foo, 2, 3)

        batch = b.run()
        batch_status = batch.status()
        assert batch_status['state'] == 'success', str((batch_status, batch.debug_info()))

    def test_update_batch_from_batch_id(self):
        b = self.batch()
        j = b.new_job()
        j.command('true')
        res = b.run()

        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

        b2 = Batch.from_batch_id(res.id, backend=b._backend)
        j2 = b2.new_job()
        j2.command('true')
        res = b2.run()
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))

    def test_python_job_with_kwarg(self):
        def foo(*, kwarg):
            return kwarg

        b = self.batch(default_python_image=PYTHON_DILL_IMAGE)
        j = b.new_python_job()
        r = j.call(foo, kwarg='hello world')

        output_path = f'{self.cloud_output_dir}/test_python_job_with_kwarg'
        b.write_output(r.as_json(), output_path)
        res = b.run()
        assert isinstance(res, bc.Batch)

        assert res.status()['state'] == 'success', str((res, res.debug_info()))
        with hfs.open(output_path) as f:
            assert orjson.loads(f.read()) == 'hello world'

    def test_tuple_recursive_resource_extraction_in_python_jobs(self):
        b = self.batch(default_python_image=PYTHON_DILL_IMAGE)

        def write(paths):
            if not isinstance(paths, tuple):
                raise ValueError('paths must be a tuple')
            for i, path in enumerate(paths):
                with open(path, 'w') as f:
                    f.write(f'{i}')

        head = b.new_python_job()
        head.call(write, (head.ofile1, head.ofile2))

        tail = b.new_bash_job()
        tail.command(f'cat {head.ofile1}')
        tail.command(f'cat {head.ofile2}')

        res = b.run()
        assert res
        assert tail._job_id
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))
        assert res.get_job_log(tail._job_id)['main'] == '01', str(res.debug_info())

    def test_list_recursive_resource_extraction_in_python_jobs(self):
        b = self.batch(default_python_image=PYTHON_DILL_IMAGE)

        def write(paths):
            for i, path in enumerate(paths):
                with open(path, 'w') as f:
                    f.write(f'{i}')

        head = b.new_python_job()
        head.call(write, [head.ofile1, head.ofile2])

        tail = b.new_bash_job()
        tail.command(f'cat {head.ofile1}')
        tail.command(f'cat {head.ofile2}')

        res = b.run()
        assert res
        assert tail._job_id
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))
        assert res.get_job_log(tail._job_id)['main'] == '01', str(res.debug_info())

    def test_dict_recursive_resource_extraction_in_python_jobs(self):
        b = self.batch(default_python_image=PYTHON_DILL_IMAGE)

        def write(kwargs):
            for k, v in kwargs.items():
                with open(v, 'w') as f:
                    f.write(k)

        head = b.new_python_job()
        head.call(write, {'a': head.ofile1, 'b': head.ofile2})

        tail = b.new_bash_job()
        tail.command(f'cat {head.ofile1}')
        tail.command(f'cat {head.ofile2}')

        res = b.run()
        assert res
        assert tail._job_id
        res_status = res.status()
        assert res_status['state'] == 'success', str((res_status, res.debug_info()))
        assert res.get_job_log(tail._job_id)['main'] == 'ab', str(res.debug_info())

    def test_wait_on_empty_batch_update(self):
        b = self.batch()
        b.run(wait=True)
        b.run(wait=True)

    def test_non_spot_job(self):
        b = self.batch()
        j = b.new_job()
        j.spot(False)
        j.command('echo hello')
        res = b.run()
        assert res is not None
        assert res.get_job(1).status()['spec']['resources']['preemptible'] == False

    def test_spot_unspecified_job(self):
        b = self.batch()
        j = b.new_job()
        j.command('echo hello')
        res = b.run()
        assert res is not None
        assert res.get_job(1).status()['spec']['resources']['preemptible'] == True

    def test_spot_true_job(self):
        b = self.batch()
        j = b.new_job()
        j.spot(True)
        j.command('echo hello')
        res = b.run()
        assert res is not None
        assert res.get_job(1).status()['spec']['resources']['preemptible'] == True

    def test_non_spot_batch(self):
        b = self.batch(default_spot=False)
        j1 = b.new_job()
        j1.command('echo hello')
        j2 = b.new_job()
        j2.command('echo hello')
        j3 = b.new_job()
        j3.spot(True)
        j3.command('echo hello')
        res = b.run()
        assert res is not None
        assert res.get_job(1).status()['spec']['resources']['preemptible'] == False
        assert res.get_job(2).status()['spec']['resources']['preemptible'] == False
        assert res.get_job(3).status()['spec']['resources']['preemptible'] == True

    def test_local_file_paths_error(self):
        b = self.batch()
        j = b.new_job()
        for input in ["hi.txt", "~/hello.csv", "./hey.tsv", "/sup.json", "file://yo.yaml"]:
            with pytest.raises(ValueError) as e:
                b.read_input(input)
            assert str(e.value).startswith("Local filepath detected")

    @skip_in_azure
    def test_validate_cloud_storage_policy(self):
        # buckets do not exist (bucket names can't contain the string "google" per
        # https://cloud.google.com/storage/docs/buckets)
        fake_bucket1 = "google"
        fake_bucket2 = "google1"
        no_bucket_error = "bucket does not exist"
        # bucket exists, but account does not have permissions on it
        no_perms_bucket = "test"
        no_perms_error = "does not have storage.buckets.get access"
        # bucket exists and account has permissions, but is set to use cold storage by default
        cold_bucket = "hail-test-cold-storage"
        cold_error = "configured to use cold storage by default"
        fake_uri1, fake_uri2, no_perms_uri, cold_uri = [
            f"gs://{bucket}/test" for bucket in [fake_bucket1, fake_bucket2, no_perms_bucket, cold_bucket]
        ]

        def _test_raises(exception_type, exception_msg, func):
            with pytest.raises(exception_type) as e:
                func()
            assert exception_msg in str(e.value)

        def _test_raises_no_bucket_error(remote_tmpdir, arg = None):
            _test_raises(ClientResponseError, no_bucket_error, lambda: ServiceBackend(remote_tmpdir=remote_tmpdir, gcs_bucket_allow_list=arg))

        def _test_raises_cold_error(func):
            _test_raises(ValueError, cold_error, func)

        # no configuration, nonexistent buckets error
        _test_raises_no_bucket_error(fake_uri1)
        _test_raises_no_bucket_error(fake_uri2)

        # no configuration, no perms bucket errors
        _test_raises(ClientResponseError, no_perms_error, lambda: ServiceBackend(remote_tmpdir=no_perms_uri))

        # no configuration, cold bucket errors
        _test_raises_cold_error(lambda: ServiceBackend(remote_tmpdir=cold_uri))
        b = self.batch()
        _test_raises_cold_error(lambda: b.read_input(cold_uri))
        j = b.new_job()
        j.command(f"echo hello > {j.ofile}")
        _test_raises_cold_error(lambda: b.write_output(j.ofile, cold_uri))

        # hailctl config, allowlisted nonexistent buckets don't error
        base_config = get_user_config()
        local_config = ConfigParser()
        local_config.read_dict({
            **{
                section: {key: val for key, val in base_config[section].items()}
                for section in base_config.sections()
            },
            **{"gcs": {"bucket_allow_list": f"{fake_bucket1},{fake_bucket2}"}}
        })
        def _get_user_config():
            return local_config
        self.monkeypatch.setattr(user_config, "get_user_config", _get_user_config)
        ServiceBackend(remote_tmpdir=fake_uri1)
        ServiceBackend(remote_tmpdir=fake_uri2)

        # environment variable config, only allowlisted nonexistent buckets don't error
        self.monkeypatch.setenv("HAIL_GCS_BUCKET_ALLOW_LIST", fake_bucket2)
        _test_raises_no_bucket_error(fake_uri1)
        ServiceBackend(remote_tmpdir=fake_uri2)

        # arg to constructor config, only allowlisted nonexistent buckets don't error
        arg = [fake_bucket1]
        ServiceBackend(remote_tmpdir=fake_uri1, gcs_bucket_allow_list=arg)
        _test_raises_no_bucket_error(fake_uri2, arg)
