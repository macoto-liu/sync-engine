import os
import time
from hashlib import sha256

from inbox.config import config
from inbox.util.stats import statsd_client
from nylas.logging import get_logger
log = get_logger()

# TODO: store AWS credentials in a better way.
STORE_MSG_ON_S3 = config.get('STORE_MESSAGES_ON_S3', None)

if STORE_MSG_ON_S3:
    import boto
    from boto.s3.connection import S3Connection
    from boto.s3.key import Key
else:
    from inbox.util.file import mkdirp

    def _data_file_directory(h):
        return os.path.join(config.get_required('MSG_PARTS_DIRECTORY'),
                            h[0], h[1], h[2], h[3], h[4], h[5])

    def _data_file_path(h):
        return os.path.join(_data_file_directory(h), h)


def save_to_blockstore(data_sha256, data):
    assert data is not None
    assert type(data) is not unicode

    if len(data) == 0:
        log.warning('Not saving 0-length data blob')
        return

    if STORE_MSG_ON_S3:
        _save_to_s3(data_sha256, data)
    else:
        directory = _data_file_directory(data_sha256)
        mkdirp(directory)

        with open(_data_file_path(data_sha256), 'wb') as f:
            f.write(data)


def _save_to_s3(data_sha256, data):
    assert 'TEMP_MESSAGE_STORE_BUCKET_NAME' in config, \
        'Need temp bucket name to store message data!'

    _save_to_s3_bucket(data_sha256,
                       config.get('TEMP_MESSAGE_STORE_BUCKET_NAME'), data)


def _save_to_s3_bucket(data_sha256, bucket_name, data):
    assert 'AWS_ACCESS_KEY_ID' in config, 'Need AWS key!'
    assert 'AWS_SECRET_ACCESS_KEY' in config, 'Need AWS secret!'
    start = time.time()

    # Boto pools connections at the class level
    conn = S3Connection(config.get('AWS_ACCESS_KEY_ID'),
                        config.get('AWS_SECRET_ACCESS_KEY'),
                        host=config.get('AWS_HOST', 's3.amazonaws.com'),
                        port=config.get('AWS_PORT'),
                        calling_format=boto.s3.connection.OrdinaryCallingFormat(),
                        is_secure=config.get('AWS_USE_SSL', True))
    bucket = conn.get_bucket(bucket_name, validate=False)

    # See if it already exists; if so, don't recreate.
    key = bucket.get_key(data_sha256)
    if key:
        return

    key = Key(bucket)
    key.key = data_sha256
    key.set_contents_from_string(data)

    end = time.time()
    latency_millis = (end - start) * 1000
    statsd_client.timing('s3_blockstore.save_latency', latency_millis)


def get_from_blockstore(data_sha256):
    if STORE_MSG_ON_S3:
        value = _get_from_s3(data_sha256)
    else:
        value = _get_from_disk(data_sha256)

    if value is None:
        # We don't store None values so if such is returned, it's an error.
        log.error('No data returned!')
        return value

    assert data_sha256 == sha256(value).hexdigest(), \
        "Returned data doesn't match stored hash!"
    return value

def _get_from_s3(data_sha256):
    assert 'AWS_ACCESS_KEY_ID' in config, 'Need AWS key!'
    assert 'AWS_SECRET_ACCESS_KEY' in config, 'Need AWS secret!'

    assert 'TEMP_MESSAGE_STORE_BUCKET_NAME' in config, \
        'Need temp bucket name to store message data!'

    # Try getting data from our temporary blockstore before
    # trying getting it from the provider.
    data = _get_from_s3_bucket(data_sha256,
                               config.get('TEMP_MESSAGE_STORE_BUCKET_NAME'))

    if data is not None:
        log.info('Found hash in temporary blockstore!',
                 sha256=data_sha256, logstash_tag='s3_direct')
        return data

    log.info("Couldn't find data in blockstore",
             sha256=data_sha256, logstash_tag='s3_direct')

    return None


def _get_from_s3_bucket(data_sha256, bucket_name):
    if not data_sha256:
        return None

    conn = S3Connection(config.get('AWS_ACCESS_KEY_ID'),
                        config.get('AWS_SECRET_ACCESS_KEY'))
    bucket = conn.get_bucket(bucket_name, validate=False)

    key = bucket.get_key(data_sha256)

    if not key:
        log.error('No key with name: {} returned!'.format(data_sha256))
        return

    return key.get_contents_as_string()


def _get_from_disk(data_sha256):
    if not data_sha256:
        return None

    try:
        with open(_data_file_path(data_sha256), 'rb') as f:
            return f.read()
    except IOError:
        log.error('No file with name: {}!'.format(data_sha256))
        return
