import io
import json

from fake_vast import offer

from crab_student import cli, core


def test_a_launch_goes_all_the_way_through_the_cli(fake_vast, fake_github, user_repo, machines, ready, monkeypatch,
                                                   capsys):  # fmt: skip
    fake_vast.offers = [offer(1, dph_total=0.30), offer(2, dph_total=0.35)]
    (user_repo / "README.md").write_text("changed\n")
    monkeypatch.chdir(user_repo)
    monkeypatch.setattr("sys.stdin", io.StringIO("# Task\nDo the thing.\n"))
    code = cli.main(["launch", "job1", "--hours", "1", "--gpus", "1", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    res = json.loads(captured.out)
    assert res["state"] == "working" and res["branch"] == "agent/job1"
    rec = core.load("job1")
    assert rec["phase"] == "ready" and rec["instance_id"] in fake_vast.instances
    assert fake_github.branch_sha("ada/proj", "agent/job1") == rec["repo"]["start_sha"]
    files = machines.uploads["job1"]
    assert files["claude.env"][1] == 0o600 and b"sk-ant-oat01-" in files["claude.env"][0]
    assert json.loads(files["config.json"][0])["branch"] == "agent/job1"
    assert len(fake_vast.calls("PUT", "/api/v0/asks/")) == 1
