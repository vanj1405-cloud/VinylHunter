import core
from providers.marketplaces import credential_status

print('Israeli stores:', len(core.store_registry()))
print('International candidates:', len(core.INTERNATIONAL_REGISTRY))
print('Marketplace credentials:', credential_status())
print('OK')
