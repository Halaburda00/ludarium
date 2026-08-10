import pytest

from ludarium import main


def test_main_prints_greeting(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    assert "ludarium" in capsys.readouterr().out.lower()
