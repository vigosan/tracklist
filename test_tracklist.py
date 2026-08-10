import hashlib
import unittest

import tracklist


def sha(value, timestamp):
    return hashlib.sha1(
        f"{timestamp} {value} https://studio.youtube.com".encode()
    ).hexdigest()


class BuildAuthorizationTest(unittest.TestCase):
    def test_binds_each_cookie_variant_to_timestamp_and_origin(self):
        cookies = {
            "SAPISID": "abc",
            "__Secure-1PAPISID": "def",
            "__Secure-3PAPISID": "ghi",
        }
        auth = tracklist.build_authorization(cookies, 1000)
        self.assertEqual(
            auth,
            f"SAPISIDHASH 1000_{sha('abc', 1000)} "
            f"SAPISID1PHASH 1000_{sha('def', 1000)} "
            f"SAPISID3PHASH 1000_{sha('ghi', 1000)}",
        )


if __name__ == "__main__":
    unittest.main()
