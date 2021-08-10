import os
import pathlib
import shutil
from unittest.mock import MagicMock

import pytest

from flytekit.core import context_manager
from flytekit.core.context_manager import ExecutionState, FlyteContextManager, Image, ImageConfig
from flytekit.core.data_persistence import FileAccessProvider
from flytekit.core.dynamic_workflow_task import dynamic
from flytekit.core.task import task
from flytekit.core.type_engine import TypeEngine
from flytekit.core.workflow import workflow
from flytekit.models.core.types import BlobType
from flytekit.models.literals import LiteralMap
from flytekit.types.directory.types import FlyteDirectory, FlyteDirToMultipartBlobTransformer


def test_engine():
    t = FlyteDirectory
    lt = TypeEngine.to_literal_type(t)
    assert lt.blob is not None
    assert lt.blob.dimensionality == BlobType.BlobDimensionality.MULTIPART
    assert lt.blob.format == ""

    t2 = FlyteDirectory["csv"]
    lt = TypeEngine.to_literal_type(t2)
    assert lt.blob is not None
    assert lt.blob.dimensionality == BlobType.BlobDimensionality.MULTIPART
    assert lt.blob.format == "csv"


def test_transformer_to_literal_local():

    random_dir = context_manager.FlyteContext.current_context().file_access.get_random_local_directory()
    fs = FileAccessProvider(local_sandbox_dir=random_dir, raw_output_prefix=os.path.join(random_dir, "raw"))
    ctx = context_manager.FlyteContext.current_context()
    with context_manager.FlyteContextManager.with_context(ctx.with_file_access(fs)) as ctx:
        # Use a separate directory that we know won't be the same as anything generated by flytekit itself, lest we
        # accidentally try to cp -R /some/folder /some/folder/sub which causes exceptions obviously.
        p = "/tmp/flyte/test_fd_transformer"

        # Create an empty directory and call to literal on it
        if os.path.exists(p):
            shutil.rmtree(p)
        pathlib.Path(p).mkdir(parents=True)

        tf = FlyteDirToMultipartBlobTransformer()
        lt = tf.get_literal_type(FlyteDirectory)
        literal = tf.to_literal(ctx, FlyteDirectory(p), FlyteDirectory, lt)
        assert literal.scalar.blob.uri.startswith(random_dir)

        # Create a director with one file in it
        if os.path.exists(p):
            shutil.rmtree(p)
        pathlib.Path(p).mkdir(parents=True)
        with open(os.path.join(p, "xyz"), "w") as fh:
            fh.write("Hello world\n")
        literal = tf.to_literal(ctx, FlyteDirectory(p), FlyteDirectory, lt)

        mock_remote_files = os.listdir(literal.scalar.blob.uri)
        assert mock_remote_files == ["xyz"]

        # The only primitives allowed are strings
        with pytest.raises(AssertionError):
            tf.to_literal(ctx, 3, FlyteDirectory, lt)

        # Can't use if it's not a directory
        with pytest.raises(AssertionError):
            p = "/tmp/flyte/xyz"
            path = pathlib.Path(p)
            try:
                path.unlink()
            except OSError:
                ...
            with open(p, "w") as fh:
                fh.write("hello world\n")
            tf.to_literal(ctx, FlyteDirectory(p), FlyteDirectory, lt)


def test_transformer_to_literal_remote():
    random_dir = context_manager.FlyteContext.current_context().file_access.get_random_local_directory()
    fs = FileAccessProvider(local_sandbox_dir=random_dir, raw_output_prefix=os.path.join(random_dir, "raw"))
    ctx = context_manager.FlyteContext.current_context()
    with context_manager.FlyteContextManager.with_context(ctx.with_file_access(fs)) as ctx:
        # Use a separate directory that we know won't be the same as anything generated by flytekit itself, lest we
        # accidentally try to cp -R /some/folder /some/folder/sub which causes exceptions obviously.
        p = "/tmp/flyte/test_fd_transformer"
        # Create an empty directory and call to literal on it
        if os.path.exists(p):
            shutil.rmtree(p)
        pathlib.Path(p).mkdir(parents=True)

        tf = FlyteDirToMultipartBlobTransformer()
        lt = tf.get_literal_type(FlyteDirectory)

        # Remote directories should be copied as is.
        literal = tf.to_literal(ctx, FlyteDirectory("s3://anything"), FlyteDirectory, lt)
        assert literal.scalar.blob.uri == "s3://anything"


def test_wf():
    @task
    def t1() -> FlyteDirectory:
        user_ctx = FlyteContextManager.current_context().user_space_params
        # Create a local directory to work with
        p = os.path.join(user_ctx.working_directory, "test_wf")
        if os.path.exists(p):
            shutil.rmtree(p)
        pathlib.Path(p).mkdir(parents=True)
        for i in range(1, 6):
            with open(os.path.join(p, f"{i}.txt"), "w") as fh:
                fh.write(f"I'm file {i}\n")

        return FlyteDirectory(p)

    d = t1()
    files = os.listdir(d.path)
    assert len(files) == 5

    @workflow
    def my_wf() -> FlyteDirectory:
        return t1()

    wfd = my_wf()
    files = os.listdir(wfd.path)
    assert len(files) == 5

    @task
    def t2(in1: FlyteDirectory["csv"]) -> int:
        return len(os.listdir(in1.path))

    @workflow
    def wf2() -> int:
        t1_dir = t1()
        y = t2(in1=t1_dir)
        return y

    x = wf2()
    assert x == 5


def test_dont_convert_remotes():
    @task
    def t1(in1: FlyteDirectory):
        print(in1)

    @dynamic
    def dyn(in1: FlyteDirectory):
        t1(in1=in1)

    fd = FlyteDirectory("s3://anything")

    ctx = context_manager.FlyteContext.current_context()
    with context_manager.FlyteContextManager.with_context(
        ctx.with_serialization_settings(
            context_manager.SerializationSettings(
                project="test_proj",
                domain="test_domain",
                version="abc",
                image_config=ImageConfig(Image(name="name", fqn="image", tag="name")),
                env={},
            )
        )
    ) as ctx:
        with context_manager.FlyteContextManager.with_context(
            ctx.with_execution_state(ctx.execution_state.with_params(mode=ExecutionState.Mode.TASK_EXECUTION))
        ) as ctx:
            lit = TypeEngine.to_literal(
                ctx, fd, FlyteDirectory, BlobType("", dimensionality=BlobType.BlobDimensionality.MULTIPART)
            )
            lm = LiteralMap(literals={"in1": lit})
            wf = dyn.dispatch_execute(ctx, lm)
            assert wf.nodes[0].inputs[0].binding.scalar.blob.uri == "s3://anything"


def test_download_caching():
    mock_downloader = MagicMock()
    f = FlyteDirectory("test", mock_downloader)
    assert not f.downloaded
    os.fspath(f)
    assert f.downloaded
    assert mock_downloader.call_count == 1
    for _ in range(10):
        os.fspath(f)
    assert mock_downloader.call_count == 1
