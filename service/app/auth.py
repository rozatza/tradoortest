"""Auth seam for a single-user runtime.

Routers that need an owner take `Depends(get_current_user)`. The dependency
returns one fixed local user. Replace the function body when a real session
exists. Do not add signup, roles, or sharing here.
"""

from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, Field

LOCAL_USER_ID = "local-user"


class CurrentUser(BaseModel):
    user_id: str = Field(min_length=1)


def get_current_user() -> CurrentUser:
    """Return the only user this process serves."""
    return CurrentUser(user_id=LOCAL_USER_ID)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
