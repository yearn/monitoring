import functools
import os
import time
from typing import Any, Callable, Dict, List, TypeVar
from urllib.parse import urlparse

from dotenv import load_dotenv
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ProviderConnectionError
from web3.providers.rpc import HTTPProvider
from web3.types import RPCResponse

from utils.logger import get_logger

from .chains import Chain

load_dotenv()

logger = get_logger("utils.web3")

T = TypeVar("T")  # Generic type for return values

# Cap on the per-attempt backoff sleep so exponential growth cannot stall a run
# for hours (a single uncapped attempt could otherwise sleep for over an hour).
MAX_BACKOFF_SECONDS = 30

# Substrings identifying deterministic contract errors. These return identically
# from every provider, so retrying or rotating providers is pointless and only
# wastes time on backoff sleeps.
#   - "execution reverted": the call reverted on-chain.
#   - "could not decode contract function call": the contract returned empty/
#     malformed data (e.g. symbol() on a non-ERC20 address). The shape mismatch
#     is a property of the contract, not the provider, so it can never succeed
#     elsewhere.
NON_RETRYABLE_ERROR_MARKERS = (
    "execution reverted",
    "could not decode contract function call",
)


def _is_non_retryable(error: Exception) -> bool:
    """Return True for deterministic errors that won't change across providers/retries."""
    msg = str(error).lower()
    return any(marker in msg for marker in NON_RETRYABLE_ERROR_MARKERS)


def retry_with_provider_rotation(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        errors = {}
        for attempt in range(self.max_retries * len(self.provider_urls)):
            try:
                return func(self, *args, **kwargs)
            except Exception as e:
                # Deterministic contract errors (e.g. reverts) can never succeed
                # on retry, so fail fast and let the caller handle it.
                if _is_non_retryable(e):
                    raise
                current_url = self.endpoint_uri
                errors[current_url] = str(e)
                logger.warning("Failed on %s: %s", current_url, e)
                time.sleep(min(self.backoff_factor * (2**attempt), MAX_BACKOFF_SECONDS))
                self._rotate_provider()

        raise ProviderConnectionError(
            "All providers failed. Errors:\n" + "\n".join(f"{url}: {err}" for url, err in errors.items())
        )

    return wrapper


class RetryProviders:
    """Base class for provider retry functionality"""

    def __init__(self, provider_urls: List[str], max_retries: int = 3, backoff_factor: float = 1):
        self.provider_urls = provider_urls
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.endpoint_uri = provider_urls[0]

    def _rotate_provider(self) -> None:
        """Switch to the next provider in the list"""
        current_index = self.provider_urls.index(self.endpoint_uri)
        next_index = (current_index + 1) % len(self.provider_urls)
        self.endpoint_uri = self.provider_urls[next_index]
        logger.warning("Switching to provider: %s", self.endpoint_uri)


class MultiHTTPProvider(HTTPProvider, RetryProviders):
    def __init__(
        self,
        providers: List[str],
        request_kwargs: Dict[str, Any] = None,
        max_retries: int = 3,
        backoff_factor: float = 1,
    ):
        providers = self._validate_urls(providers)
        RetryProviders.__init__(self, providers, max_retries, backoff_factor)
        self.request_kwargs = request_kwargs or {}
        self.request_kwargs.setdefault("timeout", 5000)
        super().__init__(endpoint_uri=self.endpoint_uri, request_kwargs=self.request_kwargs)

    def _validate_urls(self, urls: List[str]) -> List[str]:
        """Validate and filter provider URLs"""
        valid_urls = []
        for url in urls:
            if not url:
                continue
            try:
                parsed = urlparse(url)
                if not parsed.scheme or not parsed.netloc:
                    logger.warning("Invalid URL format: %s", url)
                    continue
                valid_urls.append(url)
            except Exception as e:
                logger.warning("Error validating URL %s: %s", url, e)
        return valid_urls

    @retry_with_provider_rotation
    def make_request(self, method: str, params: List[Any]) -> RPCResponse:
        return super().make_request(method, params)

    @retry_with_provider_rotation
    def make_batch_request(self, methods: List[Any]) -> List[RPCResponse]:
        return super().make_batch_request(methods)


class Web3Client(RetryProviders):
    def __init__(self, chain: Chain):
        self.chain = chain
        provider_urls = self._get_provider_urls()
        RetryProviders.__init__(self, provider_urls)
        self.w3 = self._initialize_web3()

    def _initialize_web3(self) -> Web3:
        """Initialize Web3 with multi-provider setup"""
        provider_urls = self._get_provider_urls()
        provider = MultiHTTPProvider(providers=provider_urls, max_retries=3, backoff_factor=2)
        return Web3(provider)

    def _get_provider_urls(self) -> List[str]:
        """Get provider URLs for the chain from environment variables"""
        urls = []
        # Get default provider
        env_key = f"PROVIDER_URL_{self.chain.name.upper()}"
        url = os.getenv(env_key)
        if url:
            urls.append(url)

        # Get additional providers
        for i in range(1, 4):
            env_key = f"PROVIDER_URL_{self.chain.name.upper()}_{i}"
            url = os.getenv(env_key)
            if url:
                urls.append(url)

        if not urls:
            raise ValueError(f"No providers found for chain {self.chain.name}")
        return urls

    def execute(self, operation: Callable[..., T], *args, **kwargs) -> T:
        """Execute any Web3 operation with retry logic"""
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            raise ProviderConnectionError(f"Operation failed on {self.chain.name}: {str(e)}")

    @property
    def eth(self):
        """Access to eth namespace"""
        return self.w3.eth

    def get_contract(self, address: str, abi: List[Dict]) -> Contract:
        """Get contract instance"""
        return self.w3.eth.contract(address=address, abi=abi)

    def batch_requests(self):
        return self.w3.batch_requests()

    @retry_with_provider_rotation
    def execute_batch(self, batch):
        return batch.execute()


class ChainManager:
    _instances: Dict[Chain, Web3Client] = {}

    @classmethod
    def get_client(cls, chain: Chain) -> Web3Client:
        """Get or create Web3Client instance for specified chain"""
        if chain not in cls._instances:
            cls._instances[chain] = Web3Client(chain)
        return cls._instances[chain]
