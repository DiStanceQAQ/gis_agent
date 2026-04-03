from packages.domain.services.processing_pipeline import run_processing_pipeline


def test_run_processing_pipeline_executes_clip_then_export(tmp_path) -> None:
    plan_nodes = [
        {
            "step_id": "clip1",
            "op_name": "raster.clip",
            "depends_on": [],
            "inputs": {},
            "params": {"crop": True},
            "outputs": {"raster": "r1"},
        },
        {
            "step_id": "export1",
            "op_name": "artifact.export",
            "depends_on": ["clip1"],
            "inputs": {"primary": "r1"},
            "params": {"formats": ["geotiff"]},
            "outputs": {"artifact": "a1"},
        },
    ]

    outputs = run_processing_pipeline(task_id="task_x", plan_nodes=plan_nodes, working_dir=tmp_path)
    assert "artifacts" in outputs
