import os
import socket
import time

import requests
from dotenv import load_dotenv

load_dotenv()


def test_dns_resolution():
    """Test if we can resolve the Tally API hostname"""
    try:
        ip = socket.gethostbyname("api.tally.xyz")
        print(f"DNS resolution successful: api.tally.xyz -> {ip}")
        return True
    except socket.gaierror as e:
        print(f"DNS resolution failed: {e}")
        return False


def test_basic_connectivity():
    """Test basic HTTP connectivity without API key"""
    try:
        print("Testing basic connectivity to api.tally.xyz...")
        response = requests.get("https://api.tally.xyz", timeout=10)
        print(f"Basic connection status: {response.status_code}")
        print(f"Response headers: {dict(response.headers)}")
        return True
    except requests.exceptions.Timeout:
        print("Basic connectivity timeout")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"Connection error: {e}")
        return False
    except Exception as e:
        print(f"Basic connectivity test failed: {e}")
        return False


def test_with_different_timeouts():
    """Test API with progressively longer timeouts"""
    api_key = os.getenv("TALLY_API_KEY")
    if not api_key:
        print("TALLY_API_KEY not found in environment")
        return False

    headers = {"Api-Key": api_key, "Content-Type": "application/json"}

    # Very simple query
    query = """
    query {
        chains {
            id
        }
    }
    """

    timeouts = [5, 15, 30, 60]

    for timeout in timeouts:
        print(f"\nTrying with {timeout}s timeout...")
        try:
            start_time = time.time()
            response = requests.post(
                "https://api.tally.xyz/query",
                json={"query": query},
                headers=headers,
                timeout=timeout,
            )

            elapsed = time.time() - start_time
            print(f"Response received in {elapsed:.2f}s")
            print(f"Status: {response.status_code}")

            if response.status_code == 200:
                print("Success!")
                return True
            else:
                print(f"Non-200 status: {response.text[:200]}")

        except requests.exceptions.Timeout:
            print(f"Timeout after {timeout}s")
            continue
        except Exception as e:
            print(f"Error with {timeout}s timeout: {e}")
            continue

    return False


def test_alternative_endpoints():
    """Test if there are alternative endpoints or if the main one is down"""
    api_key = os.getenv("TALLY_API_KEY")
    if not api_key:
        print("TALLY_API_KEY not found")
        return False

    # Test if we can reach the main Tally website
    try:
        print("Testing main Tally website...")
        response = requests.get("https://www.tally.xyz", timeout=10)
        print(f"Tally website status: {response.status_code}")
    except Exception as e:
        print(f"Tally website unreachable: {e}")


def test_network_with_curl():
    """Print curl command for manual testing"""
    api_key = os.getenv("TALLY_API_KEY")
    if api_key:
        curl_cmd = f"""curl -X POST https://api.tally.xyz/query \\
  -H "Api-Key: {api_key}" \\
  -H "Content-Type: application/json" \\
  -d '{{"query": "query {{ chains {{ id name }} }}"}}' \\
  --max-time 30"""

        print("\nTest manually with curl:")
        print(curl_cmd)


def main():
    print("=== Tally API Connectivity Diagnosis ===\n")

    # Step 1: DNS resolution
    print("1. Testing DNS resolution...")
    if not test_dns_resolution():
        print("❌ DNS resolution failed - check your internet connection")
        return

    # Step 2: Basic connectivity
    print("\n2. Testing basic connectivity...")
    if not test_basic_connectivity():
        print("❌ Basic connectivity failed - possible firewall/proxy issues")
        return

    # Step 3: API endpoint with different timeouts
    print("\n3. Testing API endpoint with different timeouts...")
    if test_with_different_timeouts():
        print("✅ API connection successful!")
        return

    # Step 4: Check alternatives
    print("\n4. Testing alternative endpoints...")
    test_alternative_endpoints()

    # Step 5: Manual test command
    print("\n5. Manual test option:")
    test_network_with_curl()

    print("\n❌ All automatic tests failed. Possible causes:")
    print("- Tally API is down/experiencing issues")
    print("- Your network/firewall is blocking the connection")
    print("- ISP issues with HTTPS connections to api.tally.xyz")
    print("- Try the curl command above manually")
    print("- Check Tally's status page or Discord for outages")


if __name__ == "__main__":
    main()
