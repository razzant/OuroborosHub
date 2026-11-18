#!/usr/bin/env python3
"""Checks for acquire.sh: every input against its publisher's own record or the pin the tested skill carries.

Standard library only; run as `python3 -I inputs.py <command> ...`. Each command prints one JSON record on stdout and
exits 0 when the input is what it should be, 1 when it is not, and 3 when the Web Store serves another extension build.
Nothing here runs downloaded code.
"""
import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def emit(record, code=None):
    print(json.dumps(record, indent=1, sort_keys=True))
    sys.exit(code if code is not None else (0 if record.get("ok") else 1))


def file_digest(path, name):
    digest = hashlib.new(name)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest


def dotted(data, key):
    for part in key.split("."):
        data = data[part]
    return data


def cmd_get(args):
    with open(args.json, encoding="utf-8") as handle:
        value = dotted(json.load(handle), args.key)
    print(value if isinstance(value, str) else json.dumps(value))


def skill_version(skill_md):
    with open(skill_md, encoding="utf-8") as handle:
        text = handle.read()
    front = text.split("---", 2)[1] if text.startswith("---") else ""
    for line in front.splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def cmd_hub(args):
    with open(args.inputs, encoding="utf-8") as handle:
        pins = json.load(handle)["hub"]
    files = {}
    for root, _, names in os.walk(args.skill):
        for name in sorted(names):
            path = os.path.join(root, name)
            files[os.path.relpath(path, args.skill)] = file_digest(path, "sha256").hexdigest()
    version = skill_version(os.path.join(args.skill, "SKILL.md"))
    record = {
        "part": "hub",
        "repository": pins["repository"],
        "commit": args.commit,
        "pinned_commit": pins["commit"],
        "tree": args.tree,
        "pinned_tree": pins["skill_tree"],
        "tree_matches_pin": args.tree == pins["skill_tree"],
        "skill_version": version,
        "files_sha256": dict(sorted(files.items())),
    }
    problems = []
    if not record["tree_matches_pin"]:
        problems.append("the git tree of skills/chrome_browser is %s, not the expected %s: this smoke test is written for that tree only" % (args.tree, pins["skill_tree"]))
    if version != pins["skill_version"]:
        problems.append("SKILL.md says version %s, not %s" % (version, pins["skill_version"]))
    record["ok"] = not problems
    if problems:
        record["problems"] = problems
    emit(record)


def cmd_cft(args):
    record = {"part": "cft", "version": args.version, "platform": args.platform, "url": args.url}
    problems = []
    with open(args.listing, encoding="utf-8") as handle:
        listing = json.load(handle)
    listed = None
    for entry in listing.get("versions", []):
        if entry.get("version") == args.version:
            for download in entry.get("downloads", {}).get("chrome", []):
                if download.get("platform") == args.platform:
                    listed = download.get("url")
    record["listed_url"] = listed
    if listed != args.url:
        problems.append("the official known-good listing does not name this archive for %s %s" % (args.version, args.platform))
    with open(args.meta, encoding="utf-8") as handle:
        meta = json.load(handle)
    size = os.path.getsize(args.zip)
    md5 = base64.b64encode(file_digest(args.zip, "md5").digest()).decode("ascii")
    record.update({
        "size": size,
        "gcs_size": int(meta.get("size", -1)),
        "md5_base64": md5,
        "gcs_md5_base64": meta.get("md5Hash"),
        "gcs_crc32c_base64": meta.get("crc32c"),
        "gcs_generation": meta.get("generation"),
        "gcs_updated": meta.get("updated"),
        "sha256": file_digest(args.zip, "sha256").hexdigest(),
        "sha256_note": "computed here: Google publishes size, md5 and crc32c for Chrome for Testing archives, not sha256",
    })
    if record["gcs_size"] != size:
        problems.append("the size differs from Google's object metadata")
    if record["gcs_md5_base64"] != md5:
        problems.append("the md5 differs from Google's object metadata")
    record["ok"] = not problems
    if problems:
        record["problems"] = problems
    emit(record)


def varint(buffer, position):
    result = 0
    shift = 0
    while True:
        if position >= len(buffer):
            raise ValueError("truncated varint")
        byte = buffer[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def protobuf_fields(buffer):
    fields = []
    position = 0
    while position < len(buffer):
        tag, position = varint(buffer, position)
        number, wire = tag >> 3, tag & 7
        if wire == 2:
            length, position = varint(buffer, position)
            if position + length > len(buffer):
                raise ValueError("truncated field %d" % number)
            fields.append((number, buffer[position:position + length]))
            position += length
        elif wire == 0:
            value, position = varint(buffer, position)
            fields.append((number, value))
        elif wire == 5:
            fields.append((number, buffer[position:position + 4]))
            position += 4
        elif wire == 1:
            fields.append((number, buffer[position:position + 8]))
            position += 8
        else:
            raise ValueError("unsupported wire type %d" % wire)
    return fields


def extension_id(digest16):
    return "".join(chr(ord("a") + int(c, 16)) for c in digest16.hex())


def verify_signature(proof, signed):
    """True/False when openssl answered, None when it is not available."""
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    with tempfile.TemporaryDirectory() as folder:
        key_der = os.path.join(folder, "key.der")
        key_pem = os.path.join(folder, "key.pem")
        signature = os.path.join(folder, "sig.bin")
        data = os.path.join(folder, "data.bin")
        with open(key_der, "wb") as handle:
            handle.write(proof["key"])
        with open(signature, "wb") as handle:
            handle.write(proof["signature"])
        with open(data, "wb") as handle:
            handle.write(signed)
        converted = subprocess.run([openssl, "pkey", "-pubin", "-inform", "DER", "-in", key_der, "-out", key_pem], capture_output=True)
        if converted.returncode != 0:
            return False
        checked = subprocess.run([openssl, "dgst", "-sha256", "-verify", key_pem, "-signature", signature, data], capture_output=True)
        return checked.returncode == 0


def safe_member(name):
    parts = name.split("/")
    return not (name.startswith("/") or "\\" in name or ".." in parts or any(part == "" for part in parts[:-1]))


def cmd_crx(args):
    with open(args.pins, encoding="utf-8") as handle:
        pins = json.load(handle)["extension"]
    tested = pins["tested_build"]
    expected_id = pins["id"]
    with open(args.crx, "rb") as handle:
        data = handle.read()
    record = {
        "part": "extension",
        "source": args.source,
        "id": expected_id,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "pinned_sha256": tested["crx_sha256"],
        "pin_source": "skills/chrome_browser/pins.json extension.tested_build of the tested skill",
    }
    problems = []
    try:
        if data[:4] != b"Cr24":
            raise ValueError("not a CRX file")
        version, header_length = struct.unpack("<II", data[4:12])
        if version != 3:
            raise ValueError("CRX version %d, not 3" % version)
        header = data[12:12 + header_length]
        archive = data[12 + header_length:]
        fields = protobuf_fields(header)
        signed_header = next((value for number, value in fields if number == 10000), None)
        crx_id = None
        if signed_header is not None:
            raw_id = next((value for number, value in protobuf_fields(signed_header) if number == 1), None)
            crx_id = extension_id(raw_id) if raw_id else None
        proofs = []
        for number, value in fields:
            if number in (2, 3):
                proof = dict(protobuf_fields(value))
                proofs.append({"alg": "rsa" if number == 2 else "ecdsa", "key": proof.get(1, b""), "signature": proof.get(2, b"")})
        manifest_zip = zipfile.ZipFile(io.BytesIO(archive))
        manifest = json.loads(manifest_zip.read("manifest.json"))
        key_id = extension_id(hashlib.sha256(base64.b64decode(manifest["key"])).digest()[:16]) if manifest.get("key") else None
        record.update({"crx_version": version, "signed_crx_id": crx_id, "manifest_key_id": key_id,
                       "version": manifest.get("version"), "name": manifest.get("name")})
        if crx_id != expected_id:
            problems.append("the signed crx_id is %s, not %s" % (crx_id, expected_id))
        if key_id != expected_id:
            problems.append("the manifest key gives id %s, not %s" % (key_id, expected_id))
        if manifest.get("version") != tested["version"]:
            problems.append("the extension is version %s, not %s" % (manifest.get("version"), tested["version"]))
        if signed_header is not None:
            signed = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed_header)) + signed_header + archive
            record["signatures"] = []
            for proof in proofs:
                key_id_of_proof = extension_id(hashlib.sha256(proof["key"]).digest()[:16])
                record["signatures"].append({"alg": proof["alg"], "key_id": key_id_of_proof, "verified": verify_signature(proof, signed)})
            record["developer_signature_verified"] = any(s["verified"] is True and s["key_id"] == expected_id for s in record["signatures"])
            record["signature_note"] = "checked with openssl over the CRX3 signed data; a record, not a pass condition (the sha256 pin is)"
        if record["sha256"] != tested["crx_sha256"]:
            record["ok"] = False
            record["problems"] = problems + ["the CRX is not the tested build (sha256 differs from the skill's pin): the Web Store now serves version %s" % manifest.get("version")]
            emit(record, 3)
        if os.path.exists(args.out):
            shutil.rmtree(args.out)
        os.makedirs(args.out)
        skipped = []
        for info in manifest_zip.infolist():
            name = info.filename
            if name.startswith("_metadata/"):
                skipped.append(name)
                continue
            if not safe_member(name):
                raise ValueError("unsafe path in the CRX: %r" % name)
            target = os.path.join(args.out, *name.split("/"))
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(manifest_zip.read(info))
        record["unpacked_files"] = sum(1 for info in manifest_zip.infolist() if not info.filename.endswith("/")) - len(skipped)
        record["skipped"] = skipped
        record["skipped_note"] = "Chrome refuses to load an unpacked extension with a _metadata folder (Web Store content hashes); nothing else is left out"
        files = {}
        for relative, expected in tested["files_sha256"].items():
            actual = file_digest(os.path.join(args.out, relative), "sha256").hexdigest()
            files[relative] = actual
            if actual != expected:
                problems.append("%s has sha256 %s, not the pinned %s" % (relative, actual, expected))
        record["files_sha256"] = files
    except (ValueError, KeyError, zipfile.BadZipFile, json.JSONDecodeError, OSError) as error:
        problems.append(str(error))
    record["ok"] = not problems
    if problems:
        record["problems"] = problems
    emit(record)


def npm_file(name):
    return "npm-" + name.replace("@", "").replace("/", "__") + ".tgz"


def cmd_npm_list(args):
    with open(args.pins, encoding="utf-8") as handle:
        packages = json.load(handle)["npm"]
    for name, pin in packages.items():
        print(npm_file(name), pin["tarball"])


def extract_package(data, destination):
    if os.path.exists(destination):
        shutil.rmtree(destination)
    os.makedirs(destination)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.name.startswith("package/"):
                raise ValueError("unexpected entry %r outside package/" % member.name)
            relative = member.name[len("package/"):]
            if not relative:
                continue
            if not safe_member(relative):
                raise ValueError("unsafe path %r" % member.name)
            target = os.path.join(destination, *relative.rstrip("/").split("/"))
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError("%r is not a regular file" % member.name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.extractfile(member) as source, open(target, "wb") as handle:
                handle.write(source.read())
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
            count += 1
    return count


def cmd_npm(args):
    with open(args.pins, encoding="utf-8") as handle:
        packages = json.load(handle)["npm"]
    record = {"part": "npm", "pin_source": "skills/chrome_browser/pins.json npm of the tested skill", "packages": {}}
    problems = []
    for name, pin in packages.items():
        path = os.path.join(args.dl, npm_file(name))
        entry = {"version": pin["version"], "tarball": pin["tarball"]}
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
            shasum = hashlib.sha1(data).hexdigest()
            entry.update({"bytes": len(data), "integrity_ok": integrity == pin["integrity"], "shasum_ok": shasum == pin["shasum"]})
            if not (entry["integrity_ok"] and entry["shasum_ok"]):
                raise ValueError("%s: the tarball does not match the pinned integrity and shasum" % name)
            destination = os.path.join(args.out, *name.split("/"))
            entry["files"] = extract_package(data, destination)
            with open(os.path.join(destination, "package.json"), encoding="utf-8") as handle:
                declared = json.load(handle)
            if declared.get("version") != pin["version"]:
                raise ValueError("%s: package.json says %s" % (name, declared.get("version")))
            mismatched = []
            for relative, expected in (pin.get("files_sha256") or {}).items():
                if file_digest(os.path.join(destination, relative), "sha256").hexdigest() != expected:
                    mismatched.append(relative)
            entry["pinned_files_ok"] = not mismatched
            if mismatched:
                raise ValueError("%s: pinned files differ: %s" % (name, ", ".join(mismatched)))
        except (OSError, ValueError, tarfile.TarError, json.JSONDecodeError) as error:
            problems.append(str(error))
        record["packages"][name] = entry
    record["ok"] = not problems
    if problems:
        record["problems"] = problems
    emit(record)


def cmd_problems(args):
    """Prints only the problems of a record (fixed sentences, no addresses), for a failing step's message."""
    with open(args.record, encoding="utf-8") as handle:
        record = json.load(handle)
    for problem in record.get("problems", []) or ["not verified"]:
        print("- %s" % problem)


def cmd_merge(args):
    parts = {}
    problems = []
    for path in args.parts:
        with open(path, encoding="utf-8") as handle:
            part = json.load(handle)
        parts[part["part"]] = part
        if not part.get("ok"):
            problems.extend("%s: %s" % (part["part"], problem) for problem in part.get("problems", ["not verified"]))
    merged = {"schema": 1, "ok": not problems, "problems": problems, "parts": parts}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=1, sort_keys=True)
        handle.write("\n")
    emit({"ok": merged["ok"], "problems": problems, "file": args.out})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("get")
    get.add_argument("json")
    get.add_argument("key")
    get.set_defaults(run=cmd_get)
    hub = commands.add_parser("hub")
    hub.add_argument("--inputs", required=True)
    hub.add_argument("--commit", required=True)
    hub.add_argument("--tree", required=True)
    hub.add_argument("--skill", required=True)
    hub.set_defaults(run=cmd_hub)
    cft = commands.add_parser("cft")
    for option in ("--listing", "--meta", "--zip", "--version", "--platform", "--url"):
        cft.add_argument(option, required=True)
    cft.set_defaults(run=cmd_cft)
    crx = commands.add_parser("crx")
    for option in ("--crx", "--pins", "--out", "--source"):
        crx.add_argument(option, required=True)
    crx.set_defaults(run=cmd_crx)
    npm_list = commands.add_parser("npm-list")
    npm_list.add_argument("--pins", required=True)
    npm_list.set_defaults(run=cmd_npm_list)
    npm = commands.add_parser("npm")
    for option in ("--pins", "--dl", "--out"):
        npm.add_argument(option, required=True)
    npm.set_defaults(run=cmd_npm)
    problems = commands.add_parser("problems")
    problems.add_argument("record")
    problems.set_defaults(run=cmd_problems)
    merge = commands.add_parser("merge")
    merge.add_argument("--out", required=True)
    merge.add_argument("parts", nargs="+")
    merge.set_defaults(run=cmd_merge)
    arguments = parser.parse_args()
    arguments.run(arguments)


if __name__ == "__main__":
    main()
