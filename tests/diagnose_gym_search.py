"""
Diagnostic script to test if Overpass API is working correctly with Burnell Building
"""

import sys
from core.maps_service import get_nearby_places_osm

print("=" * 70)
print("DIAGNOSTIC: Burnell Building Gym Search")
print("=" * 70)

address = "Burnell Building, Brent Cross, NW2"
amenity = "gym"

print(f"\nTesting address: {address}")
print(f"Searching for: {amenity}")
print(f"Radius: 1500m")

print(f"\nCalling get_nearby_places_osm()...\n")

try:
    places = get_nearby_places_osm(address, amenity, radius_m=1500)
    
    print(f"\nResult: {len(places)} gyms found")
    
    if places:
        print("\nDetailed results:")
        for i, place in enumerate(places, 1):
            print(f"{i}. {place['name']}")
            print(f"   Distance: {place['distance_m']}m")
            print(f"   Address: {place['address']}")
            print()
    else:
        print("No gyms found - checking if API is working...")
        
        # Try with a larger radius
        print(f"\nTrying with larger radius (3000m)...")
        places_large = get_nearby_places_osm(address, amenity, radius_m=3000)
        print(f"Result: {len(places_large)} gyms in 3km radius")
        if places_large:
            print("API is working - just no gyms within 1.5km")
        else:
            print("API might be having issues")
            
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 70)
print("DIAGNOSTIC COMPLETE")
print("=" * 70)
