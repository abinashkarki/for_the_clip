"""Google Cloud Gemini via Application Default Credentials and an explicit billing project."""
from __future__ import annotations

import os
from pathlib import Path
import re
import threading

from ..core import ModelError
from . import gemini

_LOCK = threading.Lock()
_CREDENTIALS = {}
MESSAGES = {400: 'Google Cloud rejected the model request.',
            401: 'Google Cloud sign-in expired. Run gcloud auth application-default login.',
            403: 'Check the Cloud project billing, Vertex AI API and Vertex AI User permission.',
            404: 'This model is unavailable in the selected Cloud project or location.',
            429: 'Google Cloud is busy or rate-limited. Retry shortly; check project quotas if it persists.'}


def configured():
    """Check local ADC presence without probing the network or exposing credentials."""
    explicit = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
    config = Path(os.getenv('CLOUDSDK_CONFIG', Path.home()/'.config/gcloud'))
    return Path(explicit).is_file() if explicit else (config/'application_default_credentials.json').is_file()


def headers(project, timeout):
    if not configured():
        raise ModelError('auth', 'Sign in to Google Cloud with gcloud auth application-default login first.')
    try:
        import google.auth
        from google.auth.transport.requests import Request

        class BoundedRequest(Request):
            def __call__(self, *args, **kwargs):
                kwargs['timeout'] = max(0.1, min(15, timeout))
                return super().__call__(*args, **kwargs)

        with _LOCK:
            identity = (project, os.getenv('GOOGLE_APPLICATION_CREDENTIALS'), os.getenv('CLOUDSDK_CONFIG'))
            credentials = _CREDENTIALS.get(identity)
            if credentials is None:
                credentials, _ = google.auth.default(
                    scopes=['https://www.googleapis.com/auth/cloud-platform'], quota_project_id=project)
                _CREDENTIALS[identity] = credentials
            if not credentials.valid:
                credentials.refresh(BoundedRequest())
            result = {'Content-Type': 'application/json'}
            credentials.apply(result)
            return result
    except ImportError:
        raise ModelError('config', 'Install Clippie dependencies to enable Google Cloud authentication.') from None
    except Exception:
        raise ModelError('auth', 'Could not refresh Google Cloud credentials. Run gcloud auth application-default login.') from None


def call(req, model, key):
    project = req.options.get('project', '')
    location = req.options.get('location', 'global')
    if not isinstance(project, str) or not re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]+', project):
        raise ModelError('config', 'Set an explicit Google Cloud project in this route’s options.')
    if not isinstance(location, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', location):
        raise ModelError('config', 'Choose a valid Google Cloud location.')
    if not re.fullmatch(r'[A-Za-z0-9._-]+', model):
        raise ModelError('config', 'Choose a valid Google Cloud model ID.')
    host = 'aiplatform.googleapis.com' if location == 'global' else f'{location}-aiplatform.googleapis.com'
    url = f'https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:streamGenerateContent?alt=sse'
    return gemini.stream(req, url, headers(project, req.total_timeout), 'Google Cloud', MESSAGES)
