"""
Support for ONVIF Cameras with FFmpeg as decoder.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/camera.onvif/
"""
import asyncio
import datetime as dt
import logging
import os
import voluptuous as vol

from homeassistant.const import (
    CONF_NAME, CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_PORT,
    ATTR_ENTITY_ID)
from homeassistant.components.camera import (
    Camera, PLATFORM_SCHEMA, SUPPORT_STREAM)
from homeassistant.components.camera.const import DOMAIN
from homeassistant.components.ffmpeg import (
    DATA_FFMPEG, CONF_EXTRA_ARGUMENTS)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.aiohttp_client import (
    async_aiohttp_proxy_stream)
from homeassistant.helpers.service import async_extract_entity_ids

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = 'ONVIF Camera'
DEFAULT_PORT = 5000
DEFAULT_USERNAME = 'admin'
DEFAULT_PASSWORD = '888888'
DEFAULT_ARGUMENTS = '-pred 1'
DEFAULT_PROFILE = 0
DEFAULT_SPEED = 1
DEFAULT_STEP = 0.005

CONF_PROFILE = "profile"
CONF_PTZ_SPEED = "ptz_speed"
CONF_PTZ_STEP = "ptz_step"

ATTR_PAN = "pan"
ATTR_TILT = "tilt"
ATTR_ZOOM = "zoom"
ATTR_SPEED_PAN = "speed_pan"
ATTR_SPEED_TILT = "speed_tilt"
ATTR_SPEED_ZOOM = "speed_zoom"
ATTR_DURATION = "duration"
ATTR_PRESET = "preset"

DIR_UP = "UP"
DIR_DOWN = "DOWN"
DIR_LEFT = "LEFT"
DIR_RIGHT = "RIGHT"
ZOOM_OUT = "ZOOM_OUT"
ZOOM_IN = "ZOOM_IN"
PTZ_NONE = "NONE"
STEP = 20

SERVICE_PTZ = "onvif_ptz"

ONVIF_DATA = "onvif"
ENTITIES = "entities"

TYPE_POSITIVE_EPSILON = vol.All(vol.Coerce(float), vol.Range(min=0, max=1))

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): cv.string,
    vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): cv.string,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    vol.Optional(CONF_EXTRA_ARGUMENTS, default=DEFAULT_ARGUMENTS): cv.string,
    vol.Optional(CONF_PROFILE, default=DEFAULT_PROFILE):
        vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional(CONF_PTZ_SPEED, default=DEFAULT_SPEED): vol.Any(TYPE_POSITIVE_EPSILON, vol.Schema({
        vol.Optional(ATTR_PAN, default=DEFAULT_SPEED): TYPE_POSITIVE_EPSILON,
        vol.Optional(ATTR_TILT, default=DEFAULT_SPEED): TYPE_POSITIVE_EPSILON,
        vol.Optional(ATTR_ZOOM, default=DEFAULT_SPEED): TYPE_POSITIVE_EPSILON,
    })),
    vol.Optional(CONF_PTZ_STEP, default=DEFAULT_STEP): vol.Any(TYPE_POSITIVE_EPSILON, vol.Schema({
        vol.Optional(ATTR_PAN, default=DEFAULT_STEP): TYPE_POSITIVE_EPSILON,
        vol.Optional(ATTR_TILT, default=DEFAULT_STEP): TYPE_POSITIVE_EPSILON,
        vol.Optional(ATTR_ZOOM, default=DEFAULT_STEP): TYPE_POSITIVE_EPSILON,
    })),
})

TYPE_POSITIVE_INTEGER = vol.All(vol.Coerce(int), vol.Range(min=0))

SERVICE_PTZ_SCHEMA = vol.Schema({
    ATTR_ENTITY_ID: cv.entity_ids,
    ATTR_PAN: vol.Any(TYPE_POSITIVE_INTEGER, vol.In([DIR_LEFT, DIR_RIGHT, PTZ_NONE])),
    ATTR_TILT: vol.Any(TYPE_POSITIVE_INTEGER, vol.In([DIR_UP, DIR_DOWN, PTZ_NONE])),
    ATTR_ZOOM: vol.Any(TYPE_POSITIVE_INTEGER, vol.In([ZOOM_OUT, ZOOM_IN, PTZ_NONE])),
    ATTR_DURATION: TYPE_POSITIVE_INTEGER,
    ATTR_PRESET: TYPE_POSITIVE_INTEGER,
    ATTR_SPEED_PAN: TYPE_POSITIVE_EPSILON,
    ATTR_SPEED_TILT: TYPE_POSITIVE_EPSILON,
    ATTR_SPEED_ZOOM: TYPE_POSITIVE_EPSILON,
})


async def async_setup_platform(hass, config, async_add_entities,
                               discovery_info=None):
    """Set up a ONVIF camera."""
    _LOGGER.debug("Setting up the ONVIF camera platform")

    async def async_handle_ptz(service):
        """Handle PTZ service call."""
        pan = service.data.get(ATTR_PAN, None)
        tilt = service.data.get(ATTR_TILT, None)
        zoom = service.data.get(ATTR_ZOOM, None)
        duration = service.data.get(ATTR_DURATION, None)
        speed_pan = service.data.get(ATTR_SPEED_PAN, None)
        speed_tilt = service.data.get(ATTR_SPEED_TILT, None)
        speed_zoom = service.data.get(ATTR_SPEED_ZOOM, None)
        preset = service.data.get(ATTR_PRESET, None)
        all_cameras = hass.data[ONVIF_DATA][ENTITIES]
        entity_ids = await async_extract_entity_ids(hass, service)

        _LOGGER.debug("PTZ called | Pan: %s | Tilt: %s | Zoom: %s", pan, tilt, zoom)

        pan = -STEP if pan == DIR_LEFT else STEP if pan == DIR_RIGHT else 0 if pan == PTZ_NONE or not pan else pan
        tilt = -STEP if tilt == DIR_DOWN else STEP if tilt == DIR_UP else 0 if tilt == PTZ_NONE or not tilt else tilt
        zoom = -STEP if zoom == ZOOM_OUT else STEP if zoom == ZOOM_IN else 0 if zoom == PTZ_NONE or not zoom else zoom

        _LOGGER.debug("PTZ converted | Pan: %s | Tilt: %s | Zoom: %s", pan, tilt, zoom)

        target_cameras = []
        if not entity_ids:
            target_cameras = all_cameras
        else:
            target_cameras = [camera for camera in all_cameras
                              if camera.entity_id in entity_ids]
        for camera in target_cameras:
            await camera.async_perform_ptz(pan, tilt, zoom, duration, speed_pan, speed_tilt, speed_zoom, preset)

    hass.services.async_register(DOMAIN, SERVICE_PTZ, async_handle_ptz,
                                 schema=SERVICE_PTZ_SCHEMA)

    _LOGGER.debug("Constructing the ONVIFHassCamera")

    hass_camera = ONVIFHassCamera(hass, config)

    await hass_camera.async_initialize()

    async_add_entities([hass_camera])
    return


class ONVIFHassCamera(Camera):
    """An implementation of an ONVIF camera."""

    def __init__(self, hass, config):
        """Initialize an ONVIF camera."""
        super().__init__()

        _LOGGER.debug("Importing dependencies")

        import onvif
        from onvif import ONVIFCamera

        _LOGGER.debug("Setting up the ONVIF camera component")

        self._ptz_speed = config.get(CONF_PTZ_SPEED)
        self._ptz_step = config.get(CONF_PTZ_STEP)
        self._username = config.get(CONF_USERNAME)
        self._password = config.get(CONF_PASSWORD)
        self._host = config.get(CONF_HOST)
        self._port = config.get(CONF_PORT)
        self._name = config.get(CONF_NAME)
        self._ffmpeg_arguments = config.get(CONF_EXTRA_ARGUMENTS)
        self._profile_index = config.get(CONF_PROFILE)
        self._ptz_service = None
        self._input = None

        if isinstance(self._ptz_speed, float):
            self._ptz_speed = {
                ATTR_PAN: self._ptz_speed,
                ATTR_TILT: self._ptz_speed,
                ATTR_ZOOM: self._ptz_speed,
            }

        if isinstance(self._ptz_step, float):
            self._ptz_step = {
                ATTR_PAN: self._ptz_step,
                ATTR_TILT: self._ptz_step,
                ATTR_ZOOM: self._ptz_step,
            }

        _LOGGER.debug("Setting up the ONVIF camera device @ '%s:%s'",
                      self._host,
                      self._port)

        self._camera = ONVIFCamera(self._host,
                                   self._port,
                                   self._username,
                                   self._password,
                                   '{}/wsdl/'
                                   .format(os.path.dirname(onvif.__file__)))

    async def async_initialize(self):
        """
        Initialize the camera.

        Initializes the camera by obtaining the input uri and connecting to
        the camera. Also retrieves the ONVIF profiles.
        """
        from aiohttp.client_exceptions import ClientConnectorError
        from homeassistant.exceptions import PlatformNotReady
        from zeep.exceptions import Fault
        import homeassistant.util.dt as dt_util
        from onvif import exceptions

        try:
            _LOGGER.debug("Updating service addresses")

            await self._camera.update_xaddrs()

            _LOGGER.debug("Setting up the ONVIF device management service")

            devicemgmt = self._camera.create_devicemgmt_service()

            _LOGGER.debug("Retrieving current camera date/time")

            system_date = dt_util.utcnow()
            device_time = await devicemgmt.GetSystemDateAndTime()
            if device_time:
                cdate = device_time.UTCDateTime
                cam_date = dt.datetime(cdate.Date.Year, cdate.Date.Month,
                                       cdate.Date.Day, cdate.Time.Hour,
                                       cdate.Time.Minute, cdate.Time.Second,
                                       0, dt_util.UTC)

                _LOGGER.debug("Camera date/time: %s",
                              cam_date)

                _LOGGER.debug("System date/time: %s",
                              system_date)

                dt_diff = cam_date - system_date
                dt_diff_seconds = dt_diff.total_seconds()

                if dt_diff_seconds > 5:
                    _LOGGER.warning("The date/time on the camera is '%s', "
                                    "which is different from the system '%s', "
                                    "this could lead to authentication issues",
                                    cam_date,
                                    system_date)

            _LOGGER.debug("Obtaining input uri")

            await self.async_obtain_input_uri()

            _LOGGER.debug("Setting up the ONVIF PTZ service")

            try:
                self._ptz_service = self._camera.create_ptz_service()
                _LOGGER.debug("Completed set up of the ONVIF camera component")
            except exceptions.ONVIFError as err:
                _LOGGER.warning("PTZ is not available on this camera. Error: %s", err)
        except ClientConnectorError as err:
            _LOGGER.warning("Couldn't connect to camera '%s', but will "
                            "retry later. Error: %s",
                            self._name, err)
            raise PlatformNotReady
        except Fault as err:
            _LOGGER.error("Couldn't connect to camera '%s', please verify "
                          "that the credentials are correct. Error: %s",
                          self._name, err)
        return

    async def async_obtain_profile_token(self):
        """Obtain profile token to use with requests."""
        from onvif import exceptions

        try:
            media_service = self._camera.get_service('media')

            profiles = await media_service.GetProfiles()

            _LOGGER.debug("Retrieved '%d' profiles",
                          len(profiles))

            if self._profile_index >= len(profiles):
                _LOGGER.warning("ONVIF Camera '%s' doesn't provide profile %d."
                                " Using the last profile.",
                                self._name, self._profile_index)
                self._profile_index = -1

            _LOGGER.debug("Using profile index '%d'",
                          self._profile_index)

            return profiles[self._profile_index].token
        except exceptions.ONVIFError as err:
            _LOGGER.error("Couldn't retrieve profile token of camera '%s'. Error: %s",
                          self._name, err)
            return None

    async def async_obtain_input_uri(self):
        """Set the input uri for the camera."""
        from onvif import exceptions

        _LOGGER.debug("Connecting with ONVIF Camera: %s on port %s",
                      self._host, self._port)

        try:
            _LOGGER.debug("Retrieving stream uri")

            media_service = self._camera.get_service('media')

            req = media_service.create_type('GetStreamUri')
            req.ProfileToken = await self.async_obtain_profile_token()
            req.StreamSetup = {'Stream': 'RTP-Unicast',
                               'Transport': {'Protocol': 'RTSP'}}

            stream_uri = await media_service.GetStreamUri(req)
            uri_no_auth = stream_uri.Uri
            uri_for_log = uri_no_auth.replace(
                'rtsp://', 'rtsp://<user>:<password>@', 1)
            self._input = uri_no_auth.replace(
                'rtsp://', 'rtsp://{}:{}@'.format(self._username,
                                                  self._password), 1)

            _LOGGER.debug(
                "ONVIF Camera Using the following URL for %s: %s",
                self._name, uri_for_log)
        except exceptions.ONVIFError as err:
            _LOGGER.error("Couldn't setup camera '%s'. Error: %s",
                          self._name, err)
            return

    async def async_perform_ptz(self, pan, tilt, zoom, duration, speed_pan, speed_tilt, speed_zoom, preset):
        """Perform a PTZ action on the camera."""
        from onvif import exceptions

        if self._ptz_service is None:
            _LOGGER.warning("PTZ actions are not supported on camera '%s'",
                            self._name)
            return

        if self._ptz_service:
            try:
                speed_pan_val = speed_pan if speed_pan is not None else self._ptz_speed[ATTR_PAN]
                speed_tilt_val = speed_tilt if speed_tilt is not None else self._ptz_speed[ATTR_TILT]
                speed_zoom_val = speed_zoom if speed_zoom is not None else self._ptz_speed[ATTR_ZOOM]

                _LOGGER.debug('Speeds: %d, %d, %d', speed_pan_val, speed_tilt_val, speed_zoom_val)
                _LOGGER.debug('Speed values: %s', self._ptz_speed)

                pan_val = self._ptz_step[ATTR_PAN] * pan
                tilt_val = self._ptz_step[ATTR_TILT] * tilt
                zoom_val = self._ptz_step[ATTR_ZOOM] * zoom

                _LOGGER.debug('Directions: %d, %d, %d', pan_val, tilt_val, zoom_val)
                _LOGGER.debug('Step values: %s', self._ptz_step)

                if preset is not None:
                    req = self._ptz_service.create_type('GotoPreset')
                    req.ProfileToken = await self.async_obtain_profile_token()
                    req.PresetToken = preset

                    await self._ptz_service.GotoPreset(req)
                elif duration is not None:
                    req = self._ptz_service.create_type('ContinuousMove')
                    req.ProfileToken = await self.async_obtain_profile_token()
                    req.Velocity = {
                        "PanTilt": {"x": pan_val * speed_pan_val, "y": tilt_val * speed_tilt_val},
                        "Zoom": {"x": zoom_val * speed_zoom_val},
                    }
                    # req.Timeout = 'P{}S'.format(duration)

                    _LOGGER.debug(
                        "Calling PTZ | Pan = %d | Tilt = %d | Zoom = %d | Duration = %s",
                        pan, tilt, zoom, req.Timeout
                    )

                    await self._ptz_service.ContinuousMove(req)
                    await asyncio.sleep(duration)
                    await self._ptz_service.Stop({'ProfileToken': req.ProfileToken})

                else:
                    req = self._ptz_service.create_type('RelativeMove')
                    req.ProfileToken = await self.async_obtain_profile_token()
                    req.Translation = {
                        "PanTilt": {"x": pan_val, "y": tilt_val},
                        "Zoom": {"x": zoom_val},
                    }
                    req.Speed = {
                        "PanTilt": {"x": speed_pan_val, "y": speed_tilt_val},
                        "Zoom": {"x": speed_zoom_val}
                    }

                    _LOGGER.debug(
                        "Calling PTZ | Pan = %d | Tilt = %d | Zoom = %d | Speed = %s",
                        pan_val, tilt_val, zoom_val, self._ptz_speed
                    )

                    await self._ptz_service.RelativeMove(req)

            except exceptions.ONVIFError as err:
                if "Bad Request" in err.reason:
                    self._ptz_service = None
                    _LOGGER.debug("Camera '%s' doesn't support PTZ.",
                                  self._name)
                elif "preset token" in err.reason:
                    _LOGGER.error("Camera '%s' does not have preset '%s' set up",
                                  self._name)
        else:
            _LOGGER.debug("Camera '%s' doesn't support PTZ.", self._name)

    async def async_added_to_hass(self):
        """Handle entity addition to hass."""
        _LOGGER.debug("Camera '%s' added to hass", self._name)

        if ONVIF_DATA not in self.hass.data:
            self.hass.data[ONVIF_DATA] = {}
            self.hass.data[ONVIF_DATA][ENTITIES] = []
        self.hass.data[ONVIF_DATA][ENTITIES].append(self)

    async def async_camera_image(self):
        """Return a still image response from the camera."""
        from haffmpeg.tools import ImageFrame, IMAGE_JPEG

        _LOGGER.debug("Retrieving image from camera '%s'", self._name)

        ffmpeg = ImageFrame(
            self.hass.data[DATA_FFMPEG].binary, loop=self.hass.loop)

        image = await asyncio.shield(ffmpeg.get_image(
            self._input, output_format=IMAGE_JPEG,
            extra_cmd=self._ffmpeg_arguments), loop=self.hass.loop)
        return image

    async def handle_async_mjpeg_stream(self, request):
        """Generate an HTTP MJPEG stream from the camera."""
        from haffmpeg.camera import CameraMjpeg

        _LOGGER.debug("Handling mjpeg stream from camera '%s'", self._name)

        ffmpeg_manager = self.hass.data[DATA_FFMPEG]
        stream = CameraMjpeg(ffmpeg_manager.binary,
                             loop=self.hass.loop)

        await stream.open_camera(
            self._input, extra_cmd=self._ffmpeg_arguments)

        try:
            stream_reader = await stream.get_reader()
            return await async_aiohttp_proxy_stream(
                self.hass, request, stream_reader,
                ffmpeg_manager.ffmpeg_stream_content_type)
        finally:
            await stream.close()

    @property
    def supported_features(self):
        """Return supported features."""
        if self._input:
            return SUPPORT_STREAM
        return 0

    @property
    def stream_source(self):
        """Return the stream source."""
        return self._input

    @property
    def name(self):
        """Return the name of this camera."""
        return self._name
