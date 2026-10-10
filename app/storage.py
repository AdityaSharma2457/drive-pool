from dataclasses import dataclass


@dataclass(frozen=True)
class DriveUsage:
    email: str
    display_name: str | None = None
    used_bytes: int | None = None
    limit_bytes: int | None = None
    error: str | None = None

    @property
    def available_bytes(self) -> int | None:
        if self.used_bytes is None or self.limit_bytes is None:
            return None
        return max(self.limit_bytes - self.used_bytes, 0)


def summarize_storage(drives: list[DriveUsage]) -> dict[str, int | float | bool]:
    known_usage = [drive.used_bytes for drive in drives if drive.used_bytes is not None]
    finite_drives = [
        drive
        for drive in drives
        if drive.used_bytes is not None and drive.limit_bytes is not None
    ]
    used_bytes = sum(known_usage)
    finite_used_bytes = sum(drive.used_bytes or 0 for drive in finite_drives)
    capacity_bytes = sum(drive.limit_bytes or 0 for drive in finite_drives)
    available_bytes = sum(drive.available_bytes or 0 for drive in finite_drives)

    return {
        "connected_accounts": len(drives),
        "known_accounts": len(known_usage),
        "finite_quota_accounts": len(finite_drives),
        "used_bytes": used_bytes,
        "finite_used_bytes": finite_used_bytes,
        "capacity_bytes": capacity_bytes,
        "available_bytes": available_bytes,
        "used_percent": min(finite_used_bytes / capacity_bytes * 100, 100)
        if capacity_bytes
        else 0,
        "capacity_is_complete": len(finite_drives) == len(drives),
    }