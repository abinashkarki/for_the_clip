"""Publisher interface shared by the YouTube adapter, the aggregator and future direct adapters."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

HOUR = 3600.0
# Metrics are polled this long after publication: 1 h, 6 h, 24 h, 72 h, 7 d, 28 d.
POLL_OFFSETS = (1 * HOUR, 6 * HOUR, 24 * HOUR, 72 * HOUR, 7 * 24 * HOUR, 28 * 24 * HOUR)
# Canonical metric names every adapter maps onto.
METRIC_NAMES = ('views', 'likes', 'comments', 'shares', 'saves', 'reach', 'subscribers_gained', 'watch_minutes',
                'avg_view_duration_s', 'avg_view_percentage', 'skip_rate')


class PublishError(RuntimeError):
    """A failed platform call. `retryable` errors are retried with backoff; the rest end the post as failed."""

    clear_session = False  # the saved upload session is dead; start a fresh one

    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class AuthError(PublishError):
    def __init__(self, message):
        super().__init__(message, retryable=False)


@dataclass
class ChannelInfo:
    platform: str
    display_name: str
    external_id: str
    scopes: list = field(default_factory=list)


@dataclass
class PublishResult:
    status: str  # 'scheduled' (accepted, goes live later) | 'published'
    external_id: str
    url: str | None = None
    published_at: float | None = None


@dataclass
class MetricsResult:
    metrics: dict
    retention: list | None = None  # [[elapsedVideoTimeRatio, audienceWatchRatio, relativeRetentionPerformance]]


class Publisher(ABC):
    """connect, upload, schedule, status, metrics. One instance serves one connected channel account."""
    name = 'base'

    @abstractmethod
    def connect(self, **credentials) -> list[ChannelInfo]:
        """Verify credentials and return the channels they grant."""

    @abstractmethod
    def upload(self, post: dict, media_path: str, progress=None) -> dict:
        """Send the media. Returns a JSON-serialisable handle for `schedule`; may persist `post['upload_session']`."""

    @abstractmethod
    def schedule(self, post: dict, handle: dict) -> PublishResult:
        """Publish now, or at `post['scheduled_at']`, using the handle from `upload`."""

    @abstractmethod
    def status(self, post: dict) -> PublishResult | None:
        """Current platform state of an uploaded post, None when the platform has no record of it."""

    @abstractmethod
    def metrics(self, post: dict) -> MetricsResult:
        """Latest metrics (canonical names) and, where the platform has one, the retention curve."""
