import os
import stat
from argparse import ArgumentParser
from contextlib import ExitStack as does_not_raise
from multiprocessing import Process
from pathlib import Path
from textwrap import dedent
from typing import Optional, Tuple

import numpy as np
import pytest
import xtgeo
from flaky import flaky

from ert.__main__ import ert_parser
from ert.cli import ENSEMBLE_SMOOTHER_MODE
from ert.cli.main import run_cli
from ert.config import ConfigValidationError, ErtConfig
from ert.enkf_main import create_run_path, ensemble_context, sample_prior
from ert.libres_facade import LibresFacade
from ert.storage import EnsembleAccessor, open_storage


def write_file(fname, contents):
    with open(fname, mode="w", encoding="utf-8") as fout:
        fout.writelines(contents)


def create_runpath(
    storage,
    config,
    active_mask=None,
    *,
    ensemble: Optional[EnsembleAccessor] = None,
    iteration=0,
    random_seed: Optional[int] = 1234,
) -> Tuple[ErtConfig, EnsembleAccessor]:
    active_mask = [True] if active_mask is None else active_mask
    ert_config = ErtConfig.from_file(config)

    if ensemble is None:
        experiment_id = storage.create_experiment(
            ert_config.ensemble_config.parameter_configuration
        )
        ensemble = storage.create_ensemble(
            experiment_id,
            name="default",
            ensemble_size=ert_config.model_config.num_realizations,
        )

    prior = ensemble_context(
        ensemble,
        active_mask,
        iteration,
        None,
        "",
        ert_config.model_config.runpath_format_string,
        "name",
    )

    sample_prior(
        ensemble,
        [i for i, active in enumerate(active_mask) if active],
        random_seed=random_seed,
    )
    create_run_path(prior, ert_config.substitution_list, ert_config)
    return ert_config.ensemble_config, ensemble


def load_from_forward_model(ert_config, ensemble):
    facade = LibresFacade.from_config_file(ert_config)
    realizations = [True] * facade.get_ensemble_size()
    return facade.load_from_forward_model(ensemble, realizations, 0)


@pytest.fixture
def storage(tmp_path):
    with open_storage(tmp_path / "storage", mode="w") as storage:
        yield storage


@pytest.mark.integration_test
@pytest.mark.parametrize(
    "config_str, expected, extra_files, expectation",
    [
        (
            "GEN_KW KW_NAME template.txt kw.txt prior.txt\nRANDOM_SEED 1234",
            "MY_KEYWORD -0.881423",
            [],
            does_not_raise(),
        ),
        (
            "GEN_KW KW_NAME template.txt kw.txt prior.txt INIT_FILES:custom_param%d",
            "MY_KEYWORD 1.31",
            [("custom_param0", "MY_KEYWORD 1.31")],
            does_not_raise(),
        ),
        (
            "GEN_KW KW_NAME template.txt kw.txt prior.txt INIT_FILES:custom_param%d",
            "MY_KEYWORD 1.31",
            [("custom_param0", "1.31")],
            does_not_raise(),
        ),
        (
            "GEN_KW KW_NAME template.txt kw.txt prior.txt INIT_FILES:custom_param0",  # noqa
            "Not expecting a file",
            [],
            pytest.raises(
                ConfigValidationError, match="Loading GEN_KW from files requires %d"
            ),
        ),
    ],
)
def test_gen_kw(storage, tmpdir, config_str, expected, extra_files, expectation):
    with tmpdir.as_cwd():
        config = dedent(
            """
        JOBNAME my_name%d
        NUM_REALIZATIONS 1
        """
        )
        config += config_str
        with open("config.ert", mode="w", encoding="utf-8") as fh:
            fh.writelines(config)
        with open("template.txt", mode="w", encoding="utf-8") as fh:
            fh.writelines("MY_KEYWORD <MY_KEYWORD>")
        with open("prior.txt", mode="w", encoding="utf-8") as fh:
            fh.writelines("MY_KEYWORD NORMAL 0 1")
        for fname, contents in extra_files:
            write_file(fname, contents)

        with expectation:
            create_runpath(storage, "config.ert")
            assert (
                Path("simulations/realization-0/iter-0/kw.txt").read_text(
                    encoding="utf-8"
                )
                == expected
            )


@pytest.mark.integration_test
@pytest.mark.parametrize(
    "config_str, expected, extra_files",
    [
        pytest.param(
            "GEN_KW KW_NAME template.txt kw.txt prior.txt",
            "MY_KEYWORD -0.881423\nNOT KEYWORD <DONT_REPLACE>",
            [["template.txt", "MY_KEYWORD <MY_KEYWORD>\nNOT KEYWORD <DONT_REPLACE>"]],
            id="Second magic string that should not be replaced",
        ),
        pytest.param(
            "GEN_KW KW_NAME template.txt kw.txt prior.txt",
            "MY_KEYWORD -0.881423\n-- if K<=28 then blah blah",
            [["template.txt", "MY_KEYWORD <MY_KEYWORD>\n-- if K<=28 then blah blah"]],
            id="Comment in file with <",
        ),
        pytest.param(
            "GEN_KW KW_NAME template.txt kw.txt prior.txt",
            "MY_KEYWORD -0.881423\nNR_TWO 0.654691",
            [
                ["template.txt", "MY_KEYWORD <MY_KEYWORD>\nNR_TWO <NR_TWO>"],
                ["prior.txt", "MY_KEYWORD NORMAL 0 1\nNR_TWO NORMAL 0 1"],
            ],
            id="Two parameters",
        ),
    ],
)
def test_gen_kw_templating(
    storage,
    tmpdir,
    config_str,
    expected,
    extra_files,
):
    with tmpdir.as_cwd():
        config = dedent(
            """
        JOBNAME my_name%d
        NUM_REALIZATIONS 1
        RANDOM_SEED 1234
        """
        )
        config += config_str
        with open("config.ert", mode="w", encoding="utf-8") as fh:
            fh.writelines(config)
        with open("prior.txt", mode="w", encoding="utf-8") as fh:
            fh.writelines("MY_KEYWORD NORMAL 0 1")
        for fname, contents in extra_files:
            write_file(fname, contents)
        create_runpath(storage, "config.ert")
        assert (
            Path("simulations/realization-0/iter-0/kw.txt").read_text(encoding="utf-8")
            == expected
        )


@pytest.mark.integration_test
@pytest.mark.parametrize(
    "relpath",
    [
        "somepath/",
        # This test was added to show current behaviour for Ert.
        # If absolute paths should be possible to be used like this is up for debate.
        "/tmp/somepath/",  # ert removes leading '/'
    ],
)
def test_gen_kw_outfile_will_use_paths(tmpdir, storage, relpath: str):
    with tmpdir.as_cwd():
        config = dedent(
            f"""
        JOBNAME my_name%d
        NUM_REALIZATIONS 1
        GEN_KW KW_NAME template.txt {relpath}kw.txt prior.txt
        """
        )

        with open("config.ert", mode="w", encoding="utf-8") as fh:
            fh.writelines(config)
        with open("template.txt", mode="w", encoding="utf-8") as fh:
            fh.writelines("MY_KEYWORD <MY_KEYWORD>")
        with open("prior.txt", mode="w", encoding="utf-8") as fh:
            fh.writelines("MY_KEYWORD NORMAL 0 1")
        if relpath.startswith("/"):
            relpath = relpath[1:]
        create_runpath(storage, "config.ert")
        assert os.path.exists(f"simulations/realization-0/iter-0/{relpath}kw.txt")


@pytest.mark.integration_test
@pytest.mark.parametrize(
    "config_str, expected, extra_files",
    [
        (
            "GEN_KW KW_NAME template.txt kw.txt prior.txt INIT_FILES:custom_param%d",
            "MY_KEYWORD 1.31\nMY_SECOND_KEYWORD 1.01",
            [("custom_param0", "MY_SECOND_KEYWORD 1.01\nMY_KEYWORD 1.31")],
        ),
    ],
)
def test_that_order_of_input_in_user_input_is_abritrary_for_gen_kw_init_files(
    tmpdir, config_str, expected, extra_files, storage
):
    with tmpdir.as_cwd():
        config = dedent(
            """
        JOBNAME my_name%d
        NUM_REALIZATIONS 1
        """
        )
        config += config_str
        with open("config.ert", mode="w", encoding="utf-8") as fh:
            fh.writelines(config)
        with open("template.txt", mode="w", encoding="utf-8") as fh:
            fh.writelines(
                "MY_KEYWORD <MY_KEYWORD>\nMY_SECOND_KEYWORD <MY_SECOND_KEYWORD>"
            )
        with open("prior.txt", mode="w", encoding="utf-8") as fh:
            fh.writelines("MY_KEYWORD NORMAL 0 1\nMY_SECOND_KEYWORD NORMAL 0 1")
        for fname, contents in extra_files:
            write_file(fname, contents)

        create_runpath(storage, "config.ert")
        assert (
            Path("simulations/realization-0/iter-0/kw.txt").read_text("utf-8")
            == expected
        )


@pytest.mark.integration_test
@pytest.mark.parametrize("load_forward_init", [True, False])
def test_gen_kw_forward_init(tmpdir, storage, load_forward_init):
    with tmpdir.as_cwd():
        config = dedent(
            """
        JOBNAME my_name%d
        NUM_REALIZATIONS 1
        GEN_KW KW_NAME template.txt kw.txt prior.txt """
            f"""FORWARD_INIT:{str(load_forward_init)} INIT_FILES:custom_param%d
        """
        )
        with open("config.ert", mode="w", encoding="utf-8") as fh:
            fh.writelines(config)

        with open("template.txt", mode="w", encoding="utf-8") as fh:
            fh.writelines("MY_KEYWORD <MY_KEYWORD>")
        with open("prior.txt", mode="w", encoding="utf-8") as fh:
            fh.writelines("MY_KEYWORD NORMAL 0 1")
        if not load_forward_init:
            write_file("custom_param0", "1.31")

        if load_forward_init:
            with pytest.raises(
                ConfigValidationError,
                match=(
                    "Loading GEN_KW from files created by "
                    "the forward model is not supported."
                ),
            ):
                create_runpath(storage, "config.ert")
        else:
            _, fs = create_runpath(storage, "config.ert")
            assert Path("simulations/realization-0/iter-0/kw.txt").exists()
            value = (
                fs.load_parameters("KW_NAME", 0)
                .sel(names="MY_KEYWORD")["values"]
                .values
            )
            assert value == 1.31


@pytest.mark.integration_test
def test_surface_param_update(tmpdir):
    """Full update with a surface parameter, it mirrors the poly example,
    except it uses SURFACE instead of GEN_KW.
    """
    ensemble_size = 5
    with tmpdir.as_cwd():
        config = f"""
NUM_REALIZATIONS {ensemble_size}
QUEUE_OPTION LOCAL MAX_RUNNING 5
OBS_CONFIG observations
SURFACE MY_PARAM OUTPUT_FILE:surf.irap INIT_FILES:surf.irap BASE_SURFACE:surf.irap FORWARD_INIT:True
GEN_DATA MY_RESPONSE RESULT_FILE:gen_data_%d.out REPORT_STEPS:0 INPUT_FORMAT:ASCII
INSTALL_JOB poly_eval POLY_EVAL
SIMULATION_JOB poly_eval
"""
        base_surface = xtgeo.RegularSurface(
            ncol=2,
            nrow=3,
            xinc=1,
            yinc=1,
            xori=1,
            yori=1,
            yflip=1,
            rotation=1,
        )
        base_surface.to_file("surf.irap", fformat="irap_ascii")

        with open("forward_model", "w", encoding="utf-8") as f:
            f.write(
                """#!/usr/bin/env python
import os

import xtgeo
import numpy as np

if __name__ == "__main__":
    if not os.path.exists("surf.irap"):
        nx = 2
        ny = 3
        values = np.random.standard_normal(nx * ny)
        surf = xtgeo.RegularSurface(ncol=nx,
                                    nrow=ny,
                                    xinc=1,
                                    yinc=1,
                                    rotation=0,
                                    values=values)
        surf.to_file("surf.irap", fformat="irap_ascii")

    surf_fs = xtgeo.surface_from_file("surf.irap", fformat="irap_ascii",
                                    dtype=np.float32)
    a, b, c, *_ = surf_fs.values.data.ravel()

    output = [a * x**2 + b * x + c for x in range(10)]

    with open("gen_data_0.out", "w", encoding="utf-8") as f:
        f.write("\\n".join(map(str, output)))
        """
            )
        os.chmod(
            "forward_model",
            os.stat("forward_model").st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH,
        )
        with open("POLY_EVAL", "w", encoding="utf-8") as fout:
            fout.write("EXECUTABLE forward_model")
        with open("observations", "w", encoding="utf-8") as fout:
            fout.write(
                dedent(
                    """
            GENERAL_OBSERVATION MY_OBS {
                DATA       = MY_RESPONSE;
                INDEX_LIST = 0,2,4,6,8;
                RESTART    = 0;
                OBS_FILE   = obs.txt;
            };"""
                )
            )

        with open("obs.txt", "w", encoding="utf-8") as fobs:
            fobs.write(
                dedent(
                    """
            2.1457049781272213 0.6
            8.769219841380755 1.4
            12.388014786122742 3.0
            25.600464531354252 5.4
            42.35204755970952 8.6"""
                )
            )

        with open("config.ert", "w", encoding="utf-8") as fh:
            fh.writelines(config)

        parser = ArgumentParser(prog="test_main")
        parsed = ert_parser(
            parser,
            [
                ENSEMBLE_SMOOTHER_MODE,
                "--current-case",
                "prior",
                "--target-case",
                "smoother_update",
                "config.ert",
            ],
        )

        run_cli(parsed)
        with open_storage(tmpdir / "storage") as storage:
            prior = storage.get_ensemble_by_name("prior")
            posterior = storage.get_ensemble_by_name("smoother_update")
            prior_param = (
                prior.load_parameters("MY_PARAM", range(5))["values"]
                .values.reshape(5, 2 * 3)
                .T
            )
            posterior_param = (
                posterior.load_parameters("MY_PARAM", range(5))["values"]
                .values.reshape(5, 2 * 3)
                .T
            )

            assert prior_param.dtype == np.float32
            assert posterior_param.dtype == np.float32

            assert np.linalg.det(np.cov(prior_param[:3])) > np.linalg.det(
                np.cov(posterior_param[:3])
            )

        realizations_to_test = np.random.choice(
            range(ensemble_size), size=2, replace=False
        )
        surf = xtgeo.surface_from_file(
            f"simulations/realization-{realizations_to_test[0]}/iter-1/surf.irap",
            fformat="irap_ascii",
            dtype=np.float32,
        )

        assert base_surface.ncol == surf.ncol
        assert base_surface.nrow == surf.nrow
        assert base_surface.xinc == surf.xinc
        assert base_surface.yinc == surf.yinc
        assert base_surface.xori == surf.xori
        assert base_surface.yori == surf.yori
        assert base_surface.yflip == surf.yflip
        assert base_surface.rotation == surf.yflip

        surf2 = xtgeo.surface_from_file(
            f"simulations/realization-{realizations_to_test[1]}/iter-1/surf.irap",
            fformat="irap_ascii",
            dtype=np.float32,
        )

        assert not (surf.values == surf2.values).any()

        assert len(prior.load_parameters("MY_PARAM", 0)["values"].x) == 2
        assert len(prior.load_parameters("MY_PARAM", 0)["values"].y) == 3


@pytest.mark.integration_test
@pytest.mark.limit_memory("110 MB")
@flaky(max_runs=5, min_passes=1)
def test_field_param_memory(tmpdir):
    with tmpdir.as_cwd():
        # Setup is done in a subprocess so that memray does not pick up the allocations
        p = Process(target=create_poly_with_field, args=((2000, 1000, 1), 2))
        p.start()
        p.join()  # this blocks until the process terminates

        run_poly()


def create_poly_with_field(field_dim: Tuple[int, int, int], realisations: int):
    """
    This replicates the poly example, only it uses FIELD parameter
    """
    grid_size = field_dim[0] * field_dim[1] * field_dim[2]
    config = dedent(
        f"""
            NUM_REALIZATIONS {realisations}
            OBS_CONFIG observations

            FIELD MY_PARAM PARAMETER my_param.bgrdecl INIT_FILES:my_param.bgrdecl FORWARD_INIT:True
            GRID MY_EGRID.EGRID

            GEN_DATA MY_RESPONSE RESULT_FILE:gen_data_%d.out REPORT_STEPS:0 INPUT_FORMAT:ASCII
            INSTALL_JOB poly_eval POLY_EVAL
            SIMULATION_JOB poly_eval
            """
    )
    with open("config.ert", "w", encoding="utf-8") as fh:
        fh.writelines(config)

    grid = xtgeo.create_box_grid(dimension=field_dim)
    grid.to_file("MY_EGRID.EGRID", "egrid")
    del grid

    with open("forward_model", "w", encoding="utf-8") as f:
        f.write(
            f"""#!/usr/bin/env python
import numpy as np
import os
import resfo

if __name__ == "__main__":
    if not os.path.exists("my_param.bgrdecl"):
        values = np.random.standard_normal({grid_size})
        resfo.write("my_param.bgrdecl", [("MY_PARAM", values)])
    datas = resfo.read("my_param.bgrdecl")
    assert datas[0][0] == "MY_PARAM"
    a,b,c,*_ = datas[0][1]

    output = [float(a) * x**2 + float(b) * x + float(c) for x in range(10)]
    with open("gen_data_0.out", "w", encoding="utf-8") as f:
        f.write("\\n".join(map(str, output)))
            """
        )
    os.chmod(
        "forward_model",
        os.stat("forward_model").st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
    )
    with open("POLY_EVAL", "w", encoding="utf-8") as fout:
        fout.write("EXECUTABLE forward_model")
    with open("observations", "w", encoding="utf-8") as fout:
        fout.write(
            dedent(
                """
        GENERAL_OBSERVATION MY_OBS {
            DATA       = MY_RESPONSE;
            INDEX_LIST = 0,2,4,6,8;
            RESTART    = 0;
            OBS_FILE   = obs.txt;
        };"""
            )
        )

    with open("obs.txt", "w", encoding="utf-8") as fobs:
        fobs.write(
            dedent(
                """
        2.1457049781272213 0.6
        8.769219841380755 1.4
        12.388014786122742 3.0
        25.600464531354252 5.4
        42.35204755970952 8.6"""
            )
        )


def run_poly():
    parser = ArgumentParser(prog="test_main")
    parsed = ert_parser(
        parser,
        [
            ENSEMBLE_SMOOTHER_MODE,
            "--current-case",
            "prior",
            "--target-case",
            "smoother_update",
            "config.ert",
        ],
    )

    run_cli(parsed)


@pytest.mark.integration_test
@pytest.mark.parametrize(
    "config_str, expected",
    [
        (
            "GEN_KW KW_NAME prior.txt\nRANDOM_SEED 1234",
            -0.881423,
        ),
    ],
)
def test_gen_kw_optional_template(storage, tmpdir, config_str, expected):
    with tmpdir.as_cwd():
        config = dedent(
            """
        JOBNAME my_name%d
        NUM_REALIZATIONS 1
        """
        )
        config += config_str
        with open("config.ert", mode="w", encoding="utf-8") as fh:
            fh.writelines(config)
        with open("prior.txt", mode="w", encoding="utf-8") as fh:
            fh.writelines("MY_KEYWORD NORMAL 0 1")

        create_runpath(storage, "config.ert")
        assert list(storage.ensembles)[0].load_parameters("KW_NAME")[
            "values"
        ].values.flatten().tolist() == pytest.approx([expected])
