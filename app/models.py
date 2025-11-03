from pydantic import BaseModel
from typing import Optional, Literal


class PRIdentity(BaseModel):
    installation_id: int
    owner: str
    repo: str
    number: int
    # Optional fields for logging/metrics convenience
    sender: Optional[str] = None


class WebhookHeaders(BaseModel):
    event: str
    delivery: str
    signature256: Optional[str]


class PullRequestRef(BaseModel):
    number: int
    base_ref: Optional[str]
    head_ref: Optional[str]
    head_sha: Optional[str]
    draft: Optional[bool]
    locked: Optional[bool]


class Config(BaseModel):
    label: str = "automerge"
    merge_method: Literal["squash", "rebase", "merge"] = "squash"
    update_branch: bool = True
    require_up_to_date: bool = True
    max_wait_minutes: int = 60
    poll_interval_seconds: int = 10
    title_template: str = "{title} (#{number})"
    body_template: str = "{body}\n\nAuto-merged by Auto Merge Bot for PR #{number}"
