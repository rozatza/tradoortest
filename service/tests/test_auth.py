import pytest
from pydantic import ValidationError

from app.auth import LOCAL_USER_ID, get_current_user
from app.models import StoredStrategy


def test_get_current_user_is_the_single_local_user() -> None:
    user = get_current_user()
    assert user.user_id == LOCAL_USER_ID
    assert user.user_id == "local-user"


def test_stored_strategy_requires_owner() -> None:
    with pytest.raises(ValidationError) as caught:
        StoredStrategy(strategy_id="rsi_cross_sol", ir_version="1.0", ir={})
    assert "user_id" in str(caught.value)


def test_stored_strategy_records_owner_outside_the_ir() -> None:
    row = StoredStrategy(
        user_id=get_current_user().user_id,
        strategy_id="rsi_cross_sol",
        ir_version="1.0",
        ir={"ir_version": "1.0"},
    )
    assert row.user_id == "local-user"
    assert "user_id" not in row.ir
