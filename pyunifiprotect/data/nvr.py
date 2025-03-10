"""UniFi Protect Data."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, tzinfo
from functools import cache
from ipaddress import IPv4Address, IPv6Address
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional, Union
from uuid import UUID
import zoneinfo

import aiofiles
from aiofiles import os as aos
import orjson

from pyunifiprotect.data.base import (
    ProtectBaseObject,
    ProtectDeviceModel,
    ProtectModelWithId,
)
from pyunifiprotect.data.devices import Camera, CameraZone, Light, Sensor
from pyunifiprotect.data.types import (
    AnalyticsOption,
    DoorbellMessageType,
    DoorbellText,
    EventCategories,
    EventType,
    FirmwareReleaseChannel,
    IteratorCallback,
    ModelType,
    MountType,
    PercentFloat,
    PercentInt,
    PermissionNode,
    ProgressCallback,
    RecordingType,
    ResolutionStorageType,
    SensorStatusType,
    SensorType,
    SmartDetectObjectType,
    StorageType,
    Version,
)
from pyunifiprotect.data.user import User, UserLocation
from pyunifiprotect.exceptions import BadRequest, NotAuthorized
from pyunifiprotect.utils import RELEASE_CACHE, process_datetime

try:
    from pydantic.v1.fields import PrivateAttr
except ImportError:
    from pydantic.fields import PrivateAttr

if TYPE_CHECKING:
    try:
        from pydantic.v1.typing import SetStr
    except ImportError:
        from pydantic.typing import SetStr


_LOGGER = logging.getLogger(__name__)
MAX_SUPPORTED_CAMERAS = 256
MAX_EVENT_HISTORY_IN_STATE_MACHINE = MAX_SUPPORTED_CAMERAS * 2
DELETE_KEYS_THUMB = {"color", "vehicleType"}
DELETE_KEYS_EVENT = {"deletedAt", "category", "subCategory"}


class NVRLocation(UserLocation):
    is_geofencing_enabled: bool
    radius: int
    model: Optional[ModelType] = None


class SmartDetectItem(ProtectBaseObject):
    id: str
    timestamp: datetime
    level: PercentInt
    coord: tuple[int, int, int, int]
    object_type: SmartDetectObjectType
    zone_ids: list[int]
    duration: timedelta

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {
            **super()._get_unifi_remaps(),
            "zones": "zoneIds",
        }

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        if "duration" in data:
            data["duration"] = timedelta(milliseconds=data["duration"])

        return super().unifi_dict_to_dict(data)


class SmartDetectTrack(ProtectBaseObject):
    id: str
    payload: list[SmartDetectItem]
    camera_id: str
    event_id: str

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {
            **super()._get_unifi_remaps(),
            "camera": "cameraId",
            "event": "eventId",
        }

    @property
    def camera(self) -> Camera:
        return self.api.bootstrap.cameras[self.camera_id]

    @property
    def event(self) -> Optional[Event]:
        return self.api.bootstrap.events.get(self.event_id)


class LicensePlateMetadata(ProtectBaseObject):
    name: str
    confidence_level: int


class EventThumbnailAttribute(ProtectBaseObject):
    confidence: int
    val: str


class EventThumbnailAttributes(ProtectBaseObject):
    color: Optional[EventThumbnailAttribute] = None
    vehicle_type: Optional[EventThumbnailAttribute] = None

    def unifi_dict(
        self,
        data: Optional[dict[str, Any]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        data = super().unifi_dict(data=data, exclude=exclude)

        for key in DELETE_KEYS_THUMB.intersection(data.keys()):
            if data[key] is None:
                del data[key]

        return data


class EventDetectedThumbnail(ProtectBaseObject):
    clock_best_wall: datetime
    type: str
    cropped_id: str
    attributes: Optional[EventThumbnailAttributes] = None
    name: Optional[str]

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        if "clockBestWall" in data:
            data["clockBestWall"] = process_datetime(data, "clockBestWall")

        return super().unifi_dict_to_dict(data)

    def unifi_dict(
        self,
        data: Optional[dict[str, Any]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        data = super().unifi_dict(data=data, exclude=exclude)

        if "name" in data and data["name"] is None:
            del data["name"]

        return data


class EventMetadata(ProtectBaseObject):
    client_platform: Optional[str]
    reason: Optional[str]
    app_update: Optional[str]
    light_id: Optional[str]
    light_name: Optional[str]
    type: Optional[str]
    sensor_id: Optional[str]
    sensor_name: Optional[str]
    sensor_type: Optional[SensorType]
    doorlock_id: Optional[str]
    doorlock_name: Optional[str]
    from_value: Optional[str]
    to_value: Optional[str]
    mount_type: Optional[MountType]
    status: Optional[SensorStatusType]
    alarm_type: Optional[str]
    device_id: Optional[str]
    mac: Optional[str]
    # require 2.7.5+
    license_plate: Optional[LicensePlateMetadata] = None
    # requires 2.11.13+
    detected_thumbnails: Optional[list[EventDetectedThumbnail]] = None

    _collapse_keys: ClassVar[SetStr] = {
        "lightId",
        "lightName",
        "type",
        "sensorId",
        "sensorName",
        "sensorType",
        "doorlockId",
        "doorlockName",
        "mountType",
        "status",
        "alarmType",
        "deviceId",
        "mac",
    }

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {
            **super()._get_unifi_remaps(),
            "from": "fromValue",
            "to": "toValue",
        }

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        for key in cls._collapse_keys.intersection(data.keys()):
            if isinstance(data[key], dict):
                data[key] = data[key]["text"]

        return super().unifi_dict_to_dict(data)

    def unifi_dict(
        self,
        data: Optional[dict[str, Any]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        data = super().unifi_dict(data=data, exclude=exclude)

        # all metadata keys optionally appear
        for key, value in list(data.items()):
            if value is None:
                del data[key]

        for key in self._collapse_keys.intersection(data.keys()):
            # AI Theta/Hotplug exception
            if key != "type" or data[key] not in {"audio", "video", "extender"}:
                data[key] = {"text": data[key]}

        return data


class Event(ProtectModelWithId):
    type: EventType
    start: datetime
    end: Optional[datetime]
    score: int
    heatmap_id: Optional[str]
    camera_id: Optional[str]
    smart_detect_types: list[SmartDetectObjectType]
    smart_detect_event_ids: list[str]
    thumbnail_id: Optional[str]
    user_id: Optional[str]
    timestamp: Optional[datetime]
    metadata: Optional[EventMetadata]
    # requires 2.7.5+
    deleted_at: Optional[datetime] = None
    deletion_type: Optional[Literal["manual", "automatic"]] = None
    # only appears if `get_events` is called with category
    category: Optional[EventCategories] = None
    sub_category: Optional[str] = None

    # TODO:
    # partition
    # description

    _smart_detect_events: Optional[list[Event]] = PrivateAttr(None)
    _smart_detect_track: Optional[SmartDetectTrack] = PrivateAttr(None)
    _smart_detect_zones: Optional[dict[int, CameraZone]] = PrivateAttr(None)

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {
            **super()._get_unifi_remaps(),
            "camera": "cameraId",
            "heatmap": "heatmapId",
            "user": "userId",
            "thumbnail": "thumbnailId",
            "smartDetectEvents": "smartDetectEventIds",
        }

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        for key in {"start", "end", "timestamp", "deletedAt"}.intersection(data.keys()):
            data[key] = process_datetime(data, key)

        return super().unifi_dict_to_dict(data)

    def unifi_dict(
        self,
        data: Optional[dict[str, Any]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        data = super().unifi_dict(data=data, exclude=exclude)

        for key in DELETE_KEYS_EVENT.intersection(data.keys()):
            if data[key] is None:
                del data[key]

        return data

    @property
    def camera(self) -> Optional[Camera]:
        if self.camera_id is None:
            return None

        return self.api.bootstrap.cameras.get(self.camera_id)

    @property
    def light(self) -> Optional[Light]:
        if self.metadata is None or self.metadata.light_id is None:
            return None

        return self.api.bootstrap.lights.get(self.metadata.light_id)

    @property
    def sensor(self) -> Optional[Sensor]:
        if self.metadata is None or self.metadata.sensor_id is None:
            return None

        return self.api.bootstrap.sensors.get(self.metadata.sensor_id)

    @property
    def user(self) -> Optional[User]:
        if self.user_id is None:
            return None

        return self.api.bootstrap.users.get(self.user_id)

    @property
    def smart_detect_events(self) -> list[Event]:
        if self._smart_detect_events is not None:
            return self._smart_detect_events

        self._smart_detect_events = [
            self.api.bootstrap.events[g]
            for g in self.smart_detect_event_ids
            if g in self.api.bootstrap.events
        ]
        return self._smart_detect_events

    async def get_thumbnail(
        self,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Optional[bytes]:
        """Gets thumbnail for event"""

        if self.thumbnail_id is None:
            return None
        if not self.api.bootstrap.auth_user.can(
            ModelType.CAMERA,
            PermissionNode.READ_MEDIA,
            self.camera,
        ):
            raise NotAuthorized(
                f"Do not have permission to read media for camera: {self.id}",
            )
        return await self.api.get_event_thumbnail(self.thumbnail_id, width, height)

    async def get_animated_thumbnail(
        self,
        width: Optional[int] = None,
        height: Optional[int] = None,
        *,
        speedup: int = 10,
    ) -> Optional[bytes]:
        """Gets animated thumbnail for event"""

        if self.thumbnail_id is None:
            return None
        if not self.api.bootstrap.auth_user.can(
            ModelType.CAMERA,
            PermissionNode.READ_MEDIA,
            self.camera,
        ):
            raise NotAuthorized(
                f"Do not have permission to read media for camera: {self.id}",
            )
        return await self.api.get_event_animated_thumbnail(
            self.thumbnail_id,
            width,
            height,
            speedup=speedup,
        )

    async def get_heatmap(self) -> Optional[bytes]:
        """Gets heatmap for event"""

        if self.heatmap_id is None:
            return None
        if not self.api.bootstrap.auth_user.can(
            ModelType.CAMERA,
            PermissionNode.READ_MEDIA,
            self.camera,
        ):
            raise NotAuthorized(
                f"Do not have permission to read media for camera: {self.id}",
            )
        return await self.api.get_event_heatmap(self.heatmap_id)

    async def get_video(
        self,
        channel_index: int = 0,
        output_file: Optional[Path] = None,
        iterator_callback: Optional[IteratorCallback] = None,
        progress_callback: Optional[ProgressCallback] = None,
        chunk_size: int = 65536,
    ) -> Optional[bytes]:
        """Get the MP4 video clip for this given event

        Args:
        ----
            channel_index: index of `CameraChannel` on the camera to use to retrieve video from

        Will raise an exception if event does not have a camera, end time or the channel index is wrong.
        """

        if self.camera is None:
            raise BadRequest("Event does not have a camera")
        if self.end is None:
            raise BadRequest("Event is ongoing")

        if not self.api.bootstrap.auth_user.can(
            ModelType.CAMERA,
            PermissionNode.READ_MEDIA,
            self.camera,
        ):
            raise NotAuthorized(
                f"Do not have permission to read media for camera: {self.id}",
            )
        return await self.api.get_camera_video(
            self.camera.id,
            self.start,
            self.end,
            channel_index,
            output_file=output_file,
            iterator_callback=iterator_callback,
            progress_callback=progress_callback,
            chunk_size=chunk_size,
        )

    async def get_smart_detect_track(self) -> SmartDetectTrack:
        """Gets smart detect track for given smart detect event.

        If event is not a smart detect event, it will raise a `BadRequest`
        """

        if self.type not in {EventType.SMART_DETECT, EventType.SMART_DETECT_LINE}:
            raise BadRequest("Not a smart detect event")

        if self._smart_detect_track is None:
            self._smart_detect_track = await self.api.get_event_smart_detect_track(
                self.id,
            )

        return self._smart_detect_track

    async def get_smart_detect_zones(self) -> dict[int, CameraZone]:
        """Gets the triggering zones for the smart detection"""

        if self.camera is None:
            raise BadRequest("No camera on event")

        if self._smart_detect_zones is None:
            smart_track = await self.get_smart_detect_track()

            ids: set[int] = set()
            for item in smart_track.payload:
                ids = ids | set(item.zone_ids)

            self._smart_detect_zones = {
                z.id: z for z in self.camera.smart_detect_zones if z.id in ids
            }

        return self._smart_detect_zones


class PortConfig(ProtectBaseObject):
    ump: int
    http: int
    https: int
    rtsp: int
    rtsps: int
    rtmp: int
    devices_wss: int
    camera_https: int
    camera_tcp: int
    live_ws: int
    live_wss: int
    tcp_streams: int
    playback: int
    ems_cli: int
    ems_live_flv: int
    camera_events: int
    tcp_bridge: int
    ucore: int
    discovery_client: int
    piongw: Optional[int] = None
    ems_json_cli: Optional[int] = None
    stacking: Optional[int] = None

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {
            **super()._get_unifi_remaps(),
            "emsCLI": "emsCli",
            "emsLiveFLV": "emsLiveFlv",
            "emsJsonCLI": "emsJsonCli",
        }


class CPUInfo(ProtectBaseObject):
    average_load: float
    temperature: float


class MemoryInfo(ProtectBaseObject):
    available: Optional[int]
    free: Optional[int]
    total: Optional[int]


class StorageDevice(ProtectBaseObject):
    model: str
    size: int
    healthy: Union[bool, str]


class StorageInfo(ProtectBaseObject):
    available: int
    is_recycling: bool
    size: int
    type: StorageType
    used: int
    devices: list[StorageDevice]
    # requires 2.8.14+
    capability: Optional[str] = None

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        if "type" in data:
            storage_type = data.pop("type")
            try:
                data["type"] = StorageType(storage_type)
            except ValueError:
                _LOGGER.warning("Unknown storage type: %s", storage_type)
                data["type"] = StorageType.UNKNOWN

        return super().unifi_dict_to_dict(data)


class StorageSpace(ProtectBaseObject):
    total: int
    used: int
    available: int


class TMPFSInfo(ProtectBaseObject):
    available: int
    total: int
    used: int
    path: Path


class UOSDisk(ProtectBaseObject):
    slot: int
    state: str

    type: Optional[Literal["SSD", "HDD"]] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    firmware: Optional[str] = None
    rpm: Optional[int] = None
    ata: Optional[str] = None
    sata: Optional[str] = None
    action: Optional[str] = None
    healthy: Optional[str] = None
    reason: Optional[list[Any]] = None
    temperature: Optional[int] = None
    power_on_hours: Optional[int] = None
    life_span: Optional[PercentFloat] = None
    bad_sector: Optional[int] = None
    threshold: Optional[int] = None
    progress: Optional[PercentFloat] = None
    estimate: Optional[timedelta] = None
    # 2.10.10+
    size: Optional[int] = None

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {
            **super()._get_unifi_remaps(),
            "poweronhrs": "powerOnHours",
            "life_span": "lifeSpan",
            "bad_sector": "badSector",
        }

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        if "estimate" in data and data["estimate"] is not None:
            data["estimate"] = timedelta(seconds=data.pop("estimate"))

        return super().unifi_dict_to_dict(data)

    def unifi_dict(
        self,
        data: Optional[dict[str, Any]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        data = super().unifi_dict(data=data, exclude=exclude)

        # estimate is actually in seconds, not milliseconds
        if "estimate" in data and data["estimate"] is not None:
            data["estimate"] = data["estimate"] / 1000

        if "state" in data and data["state"] == "nodisk":
            delete_keys = [
                "action",
                "ata",
                "bad_sector",
                "estimate",
                "firmware",
                "healthy",
                "life_span",
                "model",
                "poweronhrs",
                "progress",
                "reason",
                "rpm",
                "sata",
                "serial",
                "tempature",
                "temperature",
                "threshold",
                "type",
            ]
            for key in delete_keys:
                if key in data:
                    del data[key]

        return data

    @property
    def has_disk(self) -> bool:
        return self.state != "nodisk"

    @property
    def is_healthy(self) -> bool:
        return self.state in {
            "initializing",
            "expanding",
            "spare",
            "normal",
        }


class UOSSpace(ProtectBaseObject):
    device: str
    total_bytes: int
    used_bytes: int
    action: str
    progress: Optional[PercentFloat] = None
    estimate: Optional[timedelta] = None
    # requires 2.8.14+
    health: Optional[str] = None
    # requires 2.8.22+
    space_type: Optional[str] = None

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {
            **super()._get_unifi_remaps(),
            "total_bytes": "totalBytes",
            "used_bytes": "usedBytes",
            "space_type": "spaceType",
        }

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        if "estimate" in data and data["estimate"] is not None:
            data["estimate"] = timedelta(seconds=data.pop("estimate"))

        return super().unifi_dict_to_dict(data)

    def unifi_dict(
        self,
        data: Optional[dict[str, Any]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        data = super().unifi_dict(data=data, exclude=exclude)

        # esimtate is actually in seconds, not milliseconds
        if "estimate" in data and data["estimate"] is not None:
            data["estimate"] = data["estimate"] / 1000

        return data


class UOSStorage(ProtectBaseObject):
    disks: list[UOSDisk]
    space: list[UOSSpace]

    # TODO:
    # sdcards


class SystemInfo(ProtectBaseObject):
    cpu: CPUInfo
    memory: MemoryInfo
    storage: StorageInfo
    tmpfs: TMPFSInfo
    ustorage: Optional[UOSStorage] = None

    def unifi_dict(
        self,
        data: Optional[dict[str, Any]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        data = super().unifi_dict(data=data, exclude=exclude)

        if data is not None and "ustorage" in data and data["ustorage"] is None:
            del data["ustorage"]

        return data


class DoorbellMessage(ProtectBaseObject):
    type: DoorbellMessageType
    text: DoorbellText


class DoorbellSettings(ProtectBaseObject):
    default_message_text: DoorbellText
    default_message_reset_timeout: timedelta
    all_messages: list[DoorbellMessage]
    custom_messages: list[DoorbellText]

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {
            **super()._get_unifi_remaps(),
            "defaultMessageResetTimeoutMs": "defaultMessageResetTimeout",
        }

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        if "defaultMessageResetTimeoutMs" in data:
            data["defaultMessageResetTimeout"] = timedelta(
                milliseconds=data.pop("defaultMessageResetTimeoutMs"),
            )

        return super().unifi_dict_to_dict(data)


class RecordingTypeDistribution(ProtectBaseObject):
    recording_type: RecordingType
    size: int
    percentage: float


class ResolutionDistribution(ProtectBaseObject):
    resolution: ResolutionStorageType
    size: int
    percentage: float


class StorageDistribution(ProtectBaseObject):
    recording_type_distributions: list[RecordingTypeDistribution]
    resolution_distributions: list[ResolutionDistribution]

    _recording_type_dict: Optional[
        dict[RecordingType, RecordingTypeDistribution]
    ] = PrivateAttr(None)
    _resolution_dict: Optional[
        dict[ResolutionStorageType, ResolutionDistribution]
    ] = PrivateAttr(None)

    def _get_recording_type_dict(
        self,
    ) -> dict[RecordingType, RecordingTypeDistribution]:
        if self._recording_type_dict is None:
            self._recording_type_dict = {}
            for recording_type in self.recording_type_distributions:
                self._recording_type_dict[
                    recording_type.recording_type
                ] = recording_type

        return self._recording_type_dict

    def _get_resolution_dict(
        self,
    ) -> dict[ResolutionStorageType, ResolutionDistribution]:
        if self._resolution_dict is None:
            self._resolution_dict = {}
            for resolution in self.resolution_distributions:
                self._resolution_dict[resolution.resolution] = resolution

        return self._resolution_dict

    @property
    def timelapse_recordings(self) -> Optional[RecordingTypeDistribution]:
        return self._get_recording_type_dict().get(RecordingType.TIMELAPSE)

    @property
    def continuous_recordings(self) -> Optional[RecordingTypeDistribution]:
        return self._get_recording_type_dict().get(RecordingType.CONTINUOUS)

    @property
    def detections_recordings(self) -> Optional[RecordingTypeDistribution]:
        return self._get_recording_type_dict().get(RecordingType.DETECTIONS)

    @property
    def uhd_usage(self) -> Optional[ResolutionDistribution]:
        return self._get_resolution_dict().get(ResolutionStorageType.UHD)

    @property
    def hd_usage(self) -> Optional[ResolutionDistribution]:
        return self._get_resolution_dict().get(ResolutionStorageType.HD)

    @property
    def free(self) -> Optional[ResolutionDistribution]:
        return self._get_resolution_dict().get(ResolutionStorageType.FREE)

    def update_from_dict(self, data: dict[str, Any]) -> StorageDistribution:
        # reset internal look ups when data changes
        self._recording_type_dict = None
        self._resolution_dict = None

        return super().update_from_dict(data)


class StorageStats(ProtectBaseObject):
    utilization: float
    capacity: Optional[timedelta]
    remaining_capacity: Optional[timedelta]
    recording_space: StorageSpace
    storage_distribution: StorageDistribution

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        if "capacity" in data and data["capacity"] is not None:
            data["capacity"] = timedelta(milliseconds=data.pop("capacity"))
        if "remainingCapacity" in data and data["remainingCapacity"] is not None:
            data["remainingCapacity"] = timedelta(
                milliseconds=data.pop("remainingCapacity"),
            )

        return super().unifi_dict_to_dict(data)


class NVRFeatureFlags(ProtectBaseObject):
    beta: bool
    dev: bool
    notifications_v2: bool
    homekit_paired: Optional[bool] = None
    ulp_role_management: Optional[bool] = None
    # 2.9.20+
    detection_labels: Optional[bool] = None
    has_two_way_audio_media_streams: Optional[bool] = None


class NVR(ProtectDeviceModel):
    can_auto_update: bool
    is_stats_gathering_enabled: bool
    timezone: tzinfo
    version: Version
    ucore_version: str
    hardware_platform: str
    ports: PortConfig
    last_update_at: Optional[datetime]
    is_station: bool
    enable_automatic_backups: bool
    enable_stats_reporting: bool
    release_channel: FirmwareReleaseChannel
    hosts: list[Union[IPv4Address, IPv6Address, str]]
    enable_bridge_auto_adoption: bool
    hardware_id: UUID
    host_type: int
    host_shortname: str
    is_hardware: bool
    is_wireless_uplink_enabled: Optional[bool]
    time_format: Literal["12h", "24h"]
    temperature_unit: Literal["C", "F"]
    recording_retention_duration: Optional[timedelta]
    enable_crash_reporting: bool
    disable_audio: bool
    analytics_data: AnalyticsOption
    anonymous_device_id: Optional[UUID]
    camera_utilization: int
    is_recycling: bool
    disable_auto_link: bool
    skip_firmware_update: bool
    location_settings: NVRLocation
    feature_flags: NVRFeatureFlags
    system_info: SystemInfo
    doorbell_settings: DoorbellSettings
    storage_stats: StorageStats
    is_away: bool
    is_setup: bool
    network: str
    max_camera_capacity: dict[Literal["4K", "2K", "HD"], int]
    market_name: Optional[str] = None
    stream_sharing_available: Optional[bool] = None
    is_db_available: Optional[bool] = None
    is_insights_enabled: Optional[bool] = None
    is_recording_disabled: Optional[bool] = None
    is_recording_motion_only: Optional[bool] = None
    ui_version: Optional[str] = None
    sso_channel: Optional[FirmwareReleaseChannel] = None
    is_stacked: Optional[bool] = None
    is_primary: Optional[bool] = None
    last_drive_slow_event: Optional[datetime] = None
    is_u_core_setup: Optional[bool] = None
    vault_camera_ids: list[str] = []
    # requires 2.8.14+
    corruption_state: Optional[str] = None
    country_code: Optional[str] = None
    has_gateway: Optional[bool] = None
    is_vault_registered: Optional[bool] = None
    public_ip: Optional[IPv4Address] = None
    ulp_version: Optional[str] = None
    wan_ip: Optional[Union[IPv4Address, IPv6Address]] = None
    # requires 2.9.20+
    hard_drive_state: Optional[str] = None
    is_network_installed: Optional[bool] = None
    is_protect_updatable: Optional[bool] = None
    is_ucore_updatable: Optional[bool] = None
    # requires 2.11.13+
    last_device_fw_updates_checked_at: Optional[datetime] = None

    # TODO:
    # errorCode   read only
    # wifiSettings
    # smartDetectAgreement
    # dbRecoveryOptions
    # globalCameraSettings
    # portStatus
    # cameraCapacity
    # deviceFirmwareSettings

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {
            **super()._get_unifi_remaps(),
            "recordingRetentionDurationMs": "recordingRetentionDuration",
            "vaultCameras": "vaultCameraIds",
            "lastDeviceFWUpdatesCheckedAt": "lastDeviceFwUpdatesCheckedAt",
        }

    @classmethod
    @cache
    def _get_read_only_fields(cls) -> set[str]:
        return super()._get_read_only_fields() | {
            "version",
            "uiVersion",
            "hardwarePlatform",
            "ports",
            "lastUpdateAt",
            "isStation",
            "hosts",
            "hostShortname",
            "isDbAvailable",
            "isRecordingDisabled",
            "isRecordingMotionOnly",
            "cameraUtilization",
            "storageStats",
            "isRecycling",
            "avgMotions",
            "streamSharingAvailable",
        }

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        if "lastUpdateAt" in data:
            data["lastUpdateAt"] = process_datetime(data, "lastUpdateAt")
        if "lastDeviceFwUpdatesCheckedAt" in data:
            data["lastDeviceFwUpdatesCheckedAt"] = process_datetime(
                data,
                "lastDeviceFwUpdatesCheckedAt",
            )
        if (
            "recordingRetentionDurationMs" in data
            and data["recordingRetentionDurationMs"] is not None
        ):
            data["recordingRetentionDuration"] = timedelta(
                milliseconds=data.pop("recordingRetentionDurationMs"),
            )
        if "timezone" in data and not isinstance(data["timezone"], tzinfo):
            data["timezone"] = zoneinfo.ZoneInfo(data["timezone"])

        return super().unifi_dict_to_dict(data)

    async def _api_update(self, data: dict[str, Any]) -> None:
        return await self.api.update_nvr(data)

    @property
    def is_analytics_enabled(self) -> bool:
        return self.analytics_data != AnalyticsOption.NONE

    @property
    def protect_url(self) -> str:
        return f"{self.api.base_url}/protect/devices/{self.api.bootstrap.nvr.id}"

    @property
    def display_name(self) -> str:
        return self.name or self.market_name or self.type

    @property
    def vault_cameras(self) -> list[Camera]:
        """Vault Cameras for NVR"""

        if len(self.vault_camera_ids) == 0:
            return []
        return [self.api.bootstrap.cameras[c] for c in self.vault_camera_ids]

    def update_all_messages(self) -> None:
        """Updates doorbell_settings.all_messages after adding/removing custom message"""

        messages = self.doorbell_settings.custom_messages
        self.doorbell_settings.all_messages = [
            DoorbellMessage(
                type=DoorbellMessageType.LEAVE_PACKAGE_AT_DOOR,
                text=DoorbellMessageType.LEAVE_PACKAGE_AT_DOOR.value.replace("_", " "),  # type: ignore[arg-type]
            ),
            DoorbellMessage(
                type=DoorbellMessageType.DO_NOT_DISTURB,
                text=DoorbellMessageType.DO_NOT_DISTURB.value.replace("_", " "),  # type: ignore[arg-type]
            ),
            *(
                DoorbellMessage(
                    type=DoorbellMessageType.CUSTOM_MESSAGE,
                    text=message,
                )
                for message in messages
            ),
        ]

    async def set_insights(self, enabled: bool) -> None:
        """Sets analytics collection for NVR"""

        def callback() -> None:
            self.is_insights_enabled = enabled

        await self.queue_update(callback)

    async def set_analytics(self, value: AnalyticsOption) -> None:
        """Sets analytics collection for NVR"""

        def callback() -> None:
            self.analytics_data = value

        await self.queue_update(callback)

    async def set_anonymous_analytics(self, enabled: bool) -> None:
        """Enables or disables anonymous analystics for NVR"""

        if enabled:
            await self.set_analytics(AnalyticsOption.ANONYMOUS)
        else:
            await self.set_analytics(AnalyticsOption.NONE)

    async def set_default_reset_timeout(self, timeout: timedelta) -> None:
        """Sets the default message reset timeout"""

        def callback() -> None:
            self.doorbell_settings.default_message_reset_timeout = timeout

        await self.queue_update(callback)

    async def set_default_doorbell_message(self, message: str) -> None:
        """Sets default doorbell message"""

        def callback() -> None:
            self.doorbell_settings.default_message_text = DoorbellText(message)

        await self.queue_update(callback)

    async def add_custom_doorbell_message(self, message: str) -> None:
        """Adds custom doorbell message"""

        if len(message) > 30:
            raise BadRequest("Message length over 30 characters")

        if message in self.doorbell_settings.custom_messages:
            raise BadRequest("Custom doorbell message already exists")

        async with self._update_lock:
            await asyncio.sleep(
                0,
            )  # yield to the event loop once we have the look to ensure websocket updates are processed
            data_before_changes = self.dict_with_excludes()
            self.doorbell_settings.custom_messages.append(DoorbellText(message))
            await self.save_device(data_before_changes)
            self.update_all_messages()

    async def remove_custom_doorbell_message(self, message: str) -> None:
        """Removes custom doorbell message"""

        if message not in self.doorbell_settings.custom_messages:
            raise BadRequest("Custom doorbell message does not exists")

        async with self._update_lock:
            await asyncio.sleep(
                0,
            )  # yield to the event loop once we have the look to ensure websocket updates are processed
            data_before_changes = self.dict_with_excludes()
            self.doorbell_settings.custom_messages.remove(DoorbellText(message))
            await self.save_device(data_before_changes)
            self.update_all_messages()

    async def reboot(self) -> None:
        """Reboots the NVR"""

        await self.api.reboot_nvr()

    async def _read_cache_file(self, file_path: Path) -> set[Version] | None:
        versions: set[Version] | None = None

        if file_path.is_file():
            try:
                _LOGGER.debug("Reading release cache file: %s", file_path)
                async with aiofiles.open(file_path, "rb") as cache_file:
                    versions = {
                        Version(v) for v in orjson.loads(await cache_file.read())
                    }
            except Exception:
                _LOGGER.warning("Failed to parse cache file: %s", file_path)

        return versions

    async def get_is_prerelease(self) -> bool:
        """Get if current version of Protect is a prerelease version."""

        # only EA versions have `-beta` in versions
        if self.version.is_prerelease:
            return True

        # 2.6.14 is an EA version that looks like a release version
        cache_file_path = self.api.cache_dir / "release_cache.json"
        versions = await self._read_cache_file(
            cache_file_path,
        ) or await self._read_cache_file(RELEASE_CACHE)
        if versions is None or self.version not in versions:
            versions = await self.api.get_release_versions()
            try:
                _LOGGER.debug("Fetching releases from APT repos...")
                tmp = self.api.cache_dir / "release_cache.tmp.json"
                await aos.makedirs(self.api.cache_dir, exist_ok=True)
                async with aiofiles.open(tmp, "wb") as cache_file:
                    await cache_file.write(orjson.dumps([str(v) for v in versions]))
                await aos.rename(tmp, cache_file_path)
            except Exception:
                _LOGGER.warning("Failed write cache file.")

        return self.version not in versions


class LiveviewSlot(ProtectBaseObject):
    camera_ids: list[str]
    cycle_mode: str
    cycle_interval: int

    _cameras: Optional[list[Camera]] = PrivateAttr(None)

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {**super()._get_unifi_remaps(), "cameras": "cameraIds"}

    @property
    def cameras(self) -> list[Camera]:
        if self._cameras is not None:
            return self._cameras

        # user may not have permission to see the cameras in the liveview
        self._cameras = [
            self.api.bootstrap.cameras[g]
            for g in self.camera_ids
            if g in self.api.bootstrap.cameras
        ]
        return self._cameras


class Liveview(ProtectModelWithId):
    name: str
    is_default: bool
    is_global: bool
    layout: int
    slots: list[LiveviewSlot]
    owner_id: str

    @classmethod
    @cache
    def _get_unifi_remaps(cls) -> dict[str, str]:
        return {**super()._get_unifi_remaps(), "owner": "ownerId"}

    @classmethod
    @cache
    def _get_read_only_fields(cls) -> set[str]:
        return super()._get_read_only_fields() | {"isDefault", "owner"}

    @property
    def owner(self) -> Optional[User]:
        """Owner of liveview.

        Will be none if the user only has read only access and it was not made by their user.
        """

        return self.api.bootstrap.users.get(self.owner_id)

    @property
    def protect_url(self) -> str:
        return f"{self.api.base_url}/protect/liveview/{self.id}"
