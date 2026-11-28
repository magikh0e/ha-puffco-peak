import sys
from pathlib import Path
from types import ModuleType


class UpdateFailed(Exception):
    pass


class ServiceValidationError(Exception):
    pass


class DataUpdateCoordinator[T]:
    def __init__(
        self, hass, logger, *, name, update_interval, config_entry, always_update=True
    ):
        self.update_interval = update_interval
        self.data = None
        self.published = []

    def async_set_updated_data(self, data):
        self.data = data
        self.published.append(data)

    async def async_request_refresh(self):
        self.data = await self._async_update_data()


def stub(name, **attrs):
    module = ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module


stub("homeassistant.components", bluetooth=ModuleType("bluetooth"))
stub("homeassistant.config_entries", ConfigEntry=object)
stub("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
stub("homeassistant.exceptions", ServiceValidationError=ServiceValidationError)
stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=DataUpdateCoordinator,
    UpdateFailed=UpdateFailed,
)
stub(
    "bleak_retry_connector",
    BleakClientWithServiceCache=object,
    establish_connection=None,
)
stub("habluetooth", BaseHaRemoteScanner=object)
stub(
    "puffco_peak",
    __path__=[str(Path(__file__).parents[1] / "custom_components" / "puffco_peak")],
)
