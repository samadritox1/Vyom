"""Generate a fresh deployment-local key. Only the public key is printed."""
import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='.secrets/vapid_private.pem')
    args = parser.parse_args()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    # Exclusive creation prevents accidentally destroying an existing deployment key.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(private)
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    print('VAPID_PUBLIC_KEY=' + base64.urlsafe_b64encode(public).rstrip(b'=').decode())
    print('VAPID_PRIVATE_KEY_PATH=' + str(target.resolve()))


if __name__ == '__main__':
    main()
