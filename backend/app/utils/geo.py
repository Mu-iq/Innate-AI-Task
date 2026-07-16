"""Spherical geometry for pointing a Street View camera at a venue.

This module is the reason the capture stage photographs the right building. A
fixed heading, or the heading from the venue to the camera rather than the other
way round, produces a confident photograph of the shop across the road -- and
nothing downstream can detect that, because the wrong shopfront is still a
shopfront. The maths here is small, so it is also the only thing in the repo
with tests.

Earth is treated as a sphere. At the scale that matters here (<= 30m, the
MAX_PANO_DISTANCE_M cap) the error against the WGS-84 ellipsoid is well under a
centimetre, which is irrelevant next to a 640px frame at ~75 deg fov.
"""

from __future__ import annotations

import math

# Mean Earth radius (IUGG). Only used for the pano-distance check.
EARTH_RADIUS_M = 6_371_008.8


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, in degrees.

    Returns a compass bearing in [0, 360), measured clockwise from true north --
    which is exactly what the Street View Static API's `heading` parameter wants.

    Direction matters: this is the bearing FROM the panorama camera TO the venue.
    Swapping the arguments points the camera 180 degrees the wrong way, at the
    shop across the street.

        theta = atan2( sin(dlon) * cos(lat2),
                       cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon) )

    Note this is the *initial* bearing. A great-circle path's bearing changes as
    you travel it, but over <= 30m the difference is unmeasurable.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)

    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)

    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in metres.

    Used to reject panoramas further than MAX_PANO_DISTANCE_M from the venue,
    where the frontage is too small and too oblique to composite onto.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def normalise_bearing(bearing: float) -> float:
    """Wrap any bearing into [0, 360). Heading nudges routinely cross 0/360."""
    return bearing % 360.0


def nudge_heading(bearing: float, delta_deg: float) -> float:
    """Offset a heading, wrapping correctly across north.

    Used to re-shoot a frontage whose door sat at the edge of the frame.
    """
    return normalise_bearing(bearing + delta_deg)
