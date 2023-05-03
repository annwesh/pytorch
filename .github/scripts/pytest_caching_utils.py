import os
import shutil

from s3_upload_utils import *

PYTEST_CACHE_KEY_PREFIX = "pytest_cache"
PYTEST_CACHE_DIR_NAME = ".pytest_cache"
BUCKET = "pytest_cache"
TEMP_DIR = "/tmp" # a backup location in case one isn't provided

def get_sanitized_pr_identifier(pr_identifier):
    import hashlib
    # Since the default pr identifier could include user defined text (like the branch name)
    # we hash it to get a clean up the input and void corner cases
    sanitized_pr_id = hashlib.md5(pr_identifier.encode()).hexdigest()
    return sanitized_pr_id

def create_test_files_in_folder(folder, num_files):
    import random
    import string
    import os

    # make sure folder exists
    ensure_dir_exists(folder)

    for i in range(num_files):
        file_name = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        file_path = os.path.join(folder, file_name)
        with open(file_path, 'w') as f:
            f.write("This is a test file - number {}".format(i))
            print("done")
        print("Created file {}".format(file_path))

def get_s3_key_prefix(pr_identifier, workflow, job, shard=None):
    """
    The prefix to any S3 object key for a pytest cache. It's only a prefix though, not a full path to an object.
    For example, it won't include the file extension.
    """
    prefix = f"{PYTEST_CACHE_KEY_PREFIX}/{pr_identifier}/{sanitize_for_s3(workflow)}/{sanitize_for_s3(job)}"
    
    if shard:
        prefix += f"/{shard}"

    return prefix

# TODO: After uploading the cache, delete the old S3 uploads so that we don't keep downloading unnecessary files.
#       Since we want the cache to contain a union of all jobs that failed in this PR, before
#       uploading the cache we should first combine the resulting pytest cache after tests
#       with the downloaded/merged cache from before the tests.
#       However, in the short term the extra donloads are okay since they aren't that big
def upload_pytest_cache(pr_identifier, workflow, job, shard, pytest_cache_dir, bucket=BUCKET, temp_dir=TEMP_DIR):
    """
    Uploads the pytest cache to S3
    Args:
        pr_identifier: A unique, human readable identifier for the PR
        job: The name of the job that is uploading the cache
    """
    obj_key_prefix = get_s3_key_prefix(pr_identifier, workflow, job, shard)
    zip_file_path_base = f"{temp_dir}/zip-upload/{obj_key_prefix}" # doesn't include the extension

    try:
        zip_file_path = zip_folder(pytest_cache_dir, zip_file_path_base)
        obj_key = f"{obj_key_prefix}{os.path.splitext(zip_file_path)[1]}" # Keep the new file extension
        upload_file_to_s3(zip_file_path, bucket, obj_key)
    finally:
        print(f"Deleting {zip_file_path}")
        os.remove(zip_file_path)

def download_pytest_cache(pr_identifier, workflow, job, pytest_cache_dir_new, bucket=BUCKET, temp_dir=TEMP_DIR):

    obj_key_prefix = get_s3_key_prefix(pr_identifier, workflow, job)
    
    zip_download_dir = f"{temp_dir}/cache-zip-downloads/{obj_key_prefix}"
    # do the following in a try/finally block so we can clean up the temp files if something goes wrong
    try:
        # downloads the cache zips for all shards
        downloads = download_s3_objects_with_prefix(bucket, obj_key_prefix, zip_download_dir)
        
        for downloaded_zip_path in downloads:
            shard_id = os.path.splitext(os.path.basename(downloaded_zip_path))[0] # the file name of the zip is the shard id
            pytest_cache_dir_for_shard = os.path.join(f"{temp_dir}/unzipped-caches", get_s3_key_prefix(pr_identifier, workflow, job, shard_id), PYTEST_CACHE_DIR_NAME)

            try:
                unzip_folder(downloaded_zip_path, pytest_cache_dir_for_shard)
                print(f"Merging cache for job {job} shard {shard_id} into {pytest_cache_dir_new}")
                merge_pytest_caches(pytest_cache_dir_for_shard, pytest_cache_dir_new)
            finally:
                # clean up the unzipped cache folder
                shutil.rmtree(pytest_cache_dir_for_shard)
    finally:
        # clean up the downloaded zip files
        shutil.rmtree(zip_download_dir)


def copy_file(source_file, dest_file):
    ensure_dir_exists(os.path.dirname(dest_file))
    shutil.copyfile(source_file, dest_file)


def merge_pytest_caches(pytest_cache_dir_to_merge_from, pytest_cache_dir_to_merge_into):
    # Most files are identical across all caches, and we'll just use the first one we find in any of the caches
    # However, everthing in the "v/cache" folder is unique to each cache, so we'll merge those

    # Copy over all files except the 'lastfailed' file in the "v/cache" folder.
    for root, dirs, files in os.walk(pytest_cache_dir_to_merge_from):
        relative_path = os.path.relpath(root, pytest_cache_dir_to_merge_from)

        for file in files:
            if relative_path == "v/cache" and file == 'lastfailed':
                continue # We'll merge this later

            # Since these files are static, only copy them if they don't already exist in the new cache
            to_file_path = os.path.join(pytest_cache_dir_to_merge_into, relative_path, file)
            if not os.path.exists(to_file_path):
                from_file_path = os.path.join(root, file)
                copy_file(from_file_path, to_file_path)

    merge_lastfailed_files(pytest_cache_dir_to_merge_from, pytest_cache_dir_to_merge_into)

def merge_lastfailed_files(source_pytest_cache, dest_pytest_cache):
    # Simple cases where one of the files doesn't exist
    lastfailed_file_rel_path = "v/cache/lastfailed"
    source_lastfailed_file = os.path.join(source_pytest_cache, lastfailed_file_rel_path)
    dest_lastfailed_file = os.path.join(dest_pytest_cache, lastfailed_file_rel_path)

    if not os.path.exists(source_lastfailed_file):
        return
    if not os.path.exists(dest_lastfailed_file):
        copy_file(source_lastfailed_file, dest_lastfailed_file)
        return
    
    # Both files exist, so we need to merge them
    from_lastfailed = load_json_file(source_lastfailed_file)
    to_lastfailed = load_json_file(dest_lastfailed_file)
    merged_content = merged_lastfailed_content(from_lastfailed, to_lastfailed)

    # Save the results
    write_json_file(dest_lastfailed_file, merged_content)

def merged_lastfailed_content(from_lastfailed, to_lastfailed):
    """
    The lastfailed files are dictionaries where the key is the test identifier. 
    Each entry's value appears to always be `true`, but let's not count on that.
    An empty dictionary is represented with a single value with an empty string as the key.
    """
    
    # If an entry in from_lastfailed doesn't exist in to_lastfailed, add it and it's value
    for key in from_lastfailed:
        if key not in to_lastfailed:
            to_lastfailed[key] = from_lastfailed[key]

    if len(to_lastfailed) > 1:
        # Remove the empty entry if it exists since we have actual entries now
        if "" in to_lastfailed:
            del to_lastfailed[""]
    
    return to_lastfailed


if __name__ == '__main__':
    id = "b"
    folder = f"/Users/zainr/deleteme/{id}test-files"
    subfolder = f"{folder}/subfolder"
    create_test_files_in_folder(subfolder, 5)
    create_test_files_in_folder(subfolder + "2", 5)
    packaged_file_path = f"zipped_file/ffzsome-job-{id}test-files"
    packed_file = zip_folder(folder, packaged_file_path)
    print(packed_file)

    # get the packed_file path relevative to the current directory
    packed_file = os.path.relpath(packed_file, os.getcwd())
    print(packed_file)

    upload_file_to_s3(packed_file, BUCKET, packed_file)

    download_s3_objects_with_prefix(BUCKET, "zipped_file/ff", "downloaded_files")

    unzip_folder("downloaded_files/zipped_file/ffzsome-job-btest-files.zip", "/Users/zainr/deleteme/ffunzip")

    pr_identifier = get_sanitized_pr_identifier("read-deal")
    print(pr_identifier)
    workflow = "test-workflow"
    job = "test-job-name"
    shard = "shard-3"
    pytest_cache_dir = f"/Users/zainr/test-infra/{PYTEST_CACHE_DIR_NAME}"
    upload_pytest_cache(pr_identifier, workflow, job, shard, pytest_cache_dir, BUCKET)

    temp_dir = "/Users/zainr/deleteme/tmp"
    
    pytest_cache_dir_new = f"/Users/zainr/deleteme/test_pytest_cache"
    download_pytest_cache(pr_identifier, workflow, job, pytest_cache_dir_new, BUCKET)

