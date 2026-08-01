"""Groups parsed files into <=64KiB chunks on file boundaries."""

CHUNK_BYTES = 65536


def chunk_files(files):
    chunks = []
    current_chunk = None
    current_bytes = 0

    for f in files:
        if f.byte_length > CHUNK_BYTES:
            chunks.append({"files": [f]})
            current_chunk = None
            current_bytes = 0
            continue
        if current_chunk is None or current_bytes + f.byte_length > CHUNK_BYTES:
            current_chunk = {"files": []}
            chunks.append(current_chunk)
            current_bytes = 0
        current_chunk["files"].append(f)
        current_bytes += f.byte_length

    return chunks
