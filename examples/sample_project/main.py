from models import MetadataClaim

# Retained marker for the local-to-ChatGPT live-update verification.
SAMPLE_WEB_CHECK = "sample-6b3e91d2-20261004"
NATIVE_APP_CHECK = "codeconnect-49e71b6a-20261004"


def build_claims(title: str) -> list[MetadataClaim]:
    return [MetadataClaim(field="title", value=title)]
