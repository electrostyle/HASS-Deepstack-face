"""
Component that will perform facial recognition via deepstack.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/image_processing.deepstack_face
"""
import io
import logging
import re
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw

import deepstack.core as ds
import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.components.image_processing import (
    ATTR_CONFIDENCE,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_SOURCE,
    DOMAIN,
    PLATFORM_SCHEMA,
    ImageProcessingFaceEntity,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_NAME,
    CONF_IP_ADDRESS,
    CONF_PORT,
)
from homeassistant.core import split_entity_id

_LOGGER = logging.getLogger(__name__)

CONF_API_KEY = "api_key"
CONF_TIMEOUT = "timeout"
CONF_DETECT_ONLY = "detect_only"
CONF_SAVE_FILE_FOLDER = "save_file_folder"
CONF_SAVE_TIMESTAMPTED_FILE = "save_timestamped_file"

DATETIME_FORMAT = "%Y-%m-%d_%H-%M-%S"
DEFAULT_API_KEY = ""
DEFAULT_TIMEOUT = 10

CLASSIFIER = "deepstack_face"
DATA_DEEPSTACK = "deepstack_classifiers"
FILE_PATH = "file_path"
SERVICE_TEACH_FACE = "deepstack_teach_face"


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_IP_ADDRESS): cv.string,
        vol.Required(CONF_PORT): cv.port,
        vol.Optional(CONF_API_KEY, default=DEFAULT_API_KEY): cv.string,
        vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
        vol.Optional(CONF_DETECT_ONLY, default=False): cv.boolean,
        vol.Optional(CONF_SAVE_FILE_FOLDER): cv.isdir,
        vol.Optional(CONF_SAVE_TIMESTAMPTED_FILE, default=False): cv.boolean,
    }
)

SERVICE_TEACH_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(ATTR_NAME): cv.string,
        vol.Required(FILE_PATH): cv.string,
    }
)


def get_valid_filename(name: str) -> str:
    return re.sub(r"(?u)[^-\w.]", "", str(name).strip().replace(" ", "_"))


def parse_faces(predictions):
    """Get recognised faces for the image_processing.detect_face event."""
    faces = []
    for entry in predictions:
        if not "userid" in entry.keys():
            break  # we are in detect_only mode
        if entry["userid"] == "unknown":
            continue
        face = {}
        face["name"] = entry["userid"]
        face[ATTR_CONFIDENCE] = round(100.0 * entry["confidence"], 2)
        faces.append(face)
    return faces


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the classifier."""
    if DATA_DEEPSTACK not in hass.data:
        hass.data[DATA_DEEPSTACK] = []

    save_file_folder = config.get(CONF_SAVE_FILE_FOLDER)
    if save_file_folder:
        save_file_folder = Path(save_file_folder)

    entities = []
    for camera in config[CONF_SOURCE]:
        face_entity = FaceClassifyEntity(
            config[CONF_IP_ADDRESS],
            config[CONF_PORT],
            config.get(CONF_API_KEY),
            config.get(CONF_TIMEOUT),
            config.get(CONF_DETECT_ONLY),
            save_file_folder,
            config.get(CONF_SAVE_TIMESTAMPTED_FILE),
            camera[CONF_ENTITY_ID],
            camera.get(CONF_NAME),
        )
        entities.append(face_entity)
        hass.data[DATA_DEEPSTACK].append(face_entity)

    add_devices(entities)

    def service_handle(service):
        """Handle for services."""
        entity_ids = service.data.get("entity_id")

        classifiers = hass.data[DATA_DEEPSTACK]
        if entity_ids:
            classifiers = [c for c in classifiers if c.entity_id in entity_ids]

        for classifier in classifiers:
            name = service.data.get(ATTR_NAME)
            file_path = service.data.get(FILE_PATH)
            classifier.teach(name, file_path)

    hass.services.register(
        DOMAIN, SERVICE_TEACH_FACE, service_handle, schema=SERVICE_TEACH_SCHEMA
    )


class FaceClassifyEntity(ImageProcessingFaceEntity):
    """Perform a face classification."""

    def __init__(
        self,
        ip_address,
        port,
        api_key,
        timeout,
        detect_only,
        save_file_folder,
        save_timestamped_file,
        camera_entity,
        name=None,
    ):
        """Init with the API key and model id."""
        super().__init__()
        self._dsface = ds.DeepstackFace(ip_address, port, api_key, timeout)
        self._detect_only = detect_only

        self._last_detection = None
        self._save_file_folder = save_file_folder
        self._save_timestamped_file = save_timestamped_file

        self._camera = camera_entity
        if name:
            self._name = name
        else:
            camera_name = split_entity_id(camera_entity)[1]
            self._name = "{} {}".format(CLASSIFIER, camera_name)
        self._faces = []
        self._matched = {}
        self.total_faces = None

    def process_image(self, image):
        """Process an image."""
        self._faces = []
        self._matched = {}
        self.total_faces = None
        try:
            if self._detect_only:
                self._dsface.detect(image)
            else:
                self._dsface.recognise(image)
        except ds.DeepstackException as exc:
            _LOGGER.error("Depstack error : %s", exc)
            return
        self._faces = self._dsface.predictions.copy()

        if len(self._faces) > 0:
            self._last_detection = dt_util.now().strftime(DATETIME_FORMAT)
            self.total_faces = len(self._faces)
            self._matched = ds.get_recognised_faces(self._faces)
            self.process_faces(
                parse_faces(self._faces), self.total_faces
            )  # fire image_processing.detect_face
            if self._save_file_folder:
                self.save_image(
                    image, self._save_file_folder,
                )
        else:
            self.total_faces = None
            self._matched = {}

    def teach(self, name, file_path):
        """Teach classifier a face name."""
        if not self.hass.config.is_allowed_path(file_path):
            return
        with open(file_path, "rb") as image:
            self._dsface.register_face(name, image)
            _LOGGER.info("Depstack face taught name : %s", name)

    @property
    def camera_entity(self):
        """Return camera entity id from process pictures."""
        return self._camera

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Ensure consistent state."""
        return self.total_faces

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def device_state_attributes(self):
        """Return the classifier attributes."""
        attr = {}
        if self._detect_only:
            attr[CONF_DETECT_ONLY] = self._detect_only
        if not self._detect_only:
            attr["matched_faces"] = self._matched
            attr["total_matched_faces"] = len(self._matched)
        if self._last_detection:
            attr["last_target_detection"] = self._last_detection
        return attr

    def save_image(self, image, directory):
        """Draws the actual bounding box of the detected objects."""
        try:
            img = Image.open(io.BytesIO(bytearray(image))).convert("RGB")
        except UnidentifiedImageError:
            _LOGGER.warning("Deepstack unable to process image, bad data")
            return

        latest_save_path = (
            directory / f"{get_valid_filename(self._name).lower()}_latest.jpg"
        )
        img.save(latest_save_path)

        if self._save_timestamped_file:
            timestamp_save_path = (
                directory / f"{self._name}_{self._last_detection}.jpg"
            )
            img.save(timestamp_save_path)
            _LOGGER.info("Deepstack saved file %s", timestamp_save_path)
