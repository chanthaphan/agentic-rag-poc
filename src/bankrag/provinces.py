"""Thai province centroids, so a pair of coordinates can be turned into the province the locator insists on.

The bank's locator path is /Search…WithLocation/{province}/{district}/{lat}/{lon}/{type}: the coordinates order the
results, but the province segment is required - leaving it empty produces a double slash and a 404. A customer's phone
gives us coordinates and no province, so we resolve one here rather than asking them to name it.

Centroids are approximate (a few km); provinces are tens to hundreds of km apart, so the nearest centroid is the right
province for any point comfortably inside one. Near a border the answer can be the neighbour, which is why the caller
searches the two nearest and sorts the merged result by true distance - a branch across a provincial line is still a
branch near you.
"""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

# name -> (lat, lon), using the names the locator's own province list returns
CENTROIDS: dict[str, tuple[float, float]] = {
    "กรุงเทพมหานคร": (13.7563, 100.5018),
    "สมุทรปราการ": (13.5991, 100.5998),
    "นนทบุรี": (13.8591, 100.5217),
    "ปทุมธานี": (14.0208, 100.5250),
    "พระนครศรีอยุธยา": (14.3532, 100.5689),
    "อ่างทอง": (14.5896, 100.4550),
    "ลพบุรี": (14.7995, 100.6534),
    "สิงห์บุรี": (14.8879, 100.4017),
    "ชัยนาท": (15.1851, 100.1251),
    "สระบุรี": (14.5289, 100.9108),
    "ชลบุรี": (13.3611, 100.9847),
    "ระยอง": (12.6814, 101.2780),
    "จันทบุรี": (12.6112, 102.1039),
    "ตราด": (12.2428, 102.5175),
    "ฉะเชิงเทรา": (13.6904, 101.0779),
    "ปราจีนบุรี": (14.0509, 101.3660),
    "นครนายก": (14.2069, 101.2130),
    "สระแก้ว": (13.8240, 102.0645),
    "นครราชสีมา": (14.9799, 102.0977),
    "บุรีรัมย์": (14.9930, 103.1029),
    "สุรินทร์": (14.8818, 103.4936),
    "ศรีสะเกษ": (15.1186, 104.3220),
    "อุบลราชธานี": (15.2448, 104.8473),
    "ยโสธร": (15.7921, 104.1452),
    "ชัยภูมิ": (15.8068, 102.0316),
    "อำนาจเจริญ": (15.8657, 104.6259),
    "หนองบัวลำภู": (17.2218, 102.4260),
    "ขอนแก่น": (16.4322, 102.8236),
    "อุดรธานี": (17.4139, 102.7870),
    "เลย": (17.4860, 101.7223),
    "หนองคาย": (17.8783, 102.7420),
    "มหาสารคาม": (16.1852, 103.3007),
    "ร้อยเอ็ด": (16.0538, 103.6520),
    "กาฬสินธุ์": (16.4322, 103.5061),
    "สกลนคร": (17.1664, 104.1486),
    "นครพนม": (17.4108, 104.7784),
    "มุกดาหาร": (16.5420, 104.7208),
    "บึงกาฬ": (18.3609, 103.6466),
    "เชียงใหม่": (18.7883, 98.9853),
    "ลำพูน": (18.5745, 99.0087),
    "ลำปาง": (18.2888, 99.4909),
    "อุตรดิตถ์": (17.6200, 100.0993),
    "แพร่": (18.1445, 100.1405),
    "น่าน": (18.7756, 100.7730),
    "พะเยา": (19.1664, 99.9003),
    "เชียงราย": (19.9105, 99.8406),
    "แม่ฮ่องสอน": (19.3020, 97.9654),
    "นครสวรรค์": (15.7047, 100.1372),
    "อุทัยธานี": (15.3835, 100.0246),
    "กำแพงเพชร": (16.4827, 99.5226),
    "ตาก": (16.8839, 99.1258),
    "สุโขทัย": (17.0068, 99.8265),
    "พิษณุโลก": (16.8211, 100.2659),
    "พิจิตร": (16.4429, 100.3487),
    "เพชรบูรณ์": (16.4190, 101.1591),
    "ราชบุรี": (13.5283, 99.8134),
    "กาญจนบุรี": (14.0227, 99.5328),
    "สุพรรณบุรี": (14.4745, 100.1177),
    "นครปฐม": (13.8199, 100.0621),
    "สมุทรสาคร": (13.5475, 100.2745),
    "สมุทรสงคราม": (13.4098, 100.0022),
    "เพชรบุรี": (13.1119, 99.9399),
    "ประจวบคีรีขันธ์": (11.8126, 99.7957),
    "นครศรีธรรมราช": (8.4304, 99.9631),
    "กระบี่": (8.0863, 98.9063),
    "พังงา": (8.4501, 98.5255),
    "ภูเก็ต": (7.8804, 98.3923),
    "สุราษฎร์ธานี": (9.1382, 99.3215),
    "ระนอง": (9.9529, 98.6085),
    "ชุมพร": (10.4930, 99.1800),
    "สงขลา": (7.1897, 100.5951),
    "สตูล": (6.6238, 100.0673),
    "ตรัง": (7.5593, 99.6114),
    "พัทลุง": (7.6167, 100.0742),
    "ปัตตานี": (6.8692, 101.2502),
    "ยะลา": (6.5413, 101.2803),
    "นราธิวาส": (6.4254, 101.8253),
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    r = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def nearest(lat: float, lon: float, n: int = 2) -> list[str]:
    """The n provinces whose centroid is closest, nearest first."""
    ranked = sorted(CENTROIDS, key=lambda name: haversine_km(lat, lon, *CENTROIDS[name]))
    return ranked[:n]
