import requests

def verify_kash_license(license_key, instance_name="mechanic_pc"):
    url = "https://api.lemonsqueezy.com/v1/licenses/activate"
    payload = {
        "license_key": license_key,
        "instance_name": instance_name # Tracks the specific computer using it
    }
    headers = {
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        data = response.json()
        
        # Check if the license is active and valid
        if data.get("activated") and data.get("license_key", {}).get("status") == "active":
            print("Access Granted: KASH Diagnostics Initializing...")
            return True
        else:
            print("Access Denied: Invalid or Expired Subscription.")
            return False
    except Exception as e:
        print("Connection error checking license.")
        return False
