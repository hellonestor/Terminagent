#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
version=${1:-2.1.5-agent4}

if [[ ! $version =~ ^[0-9A-Za-z.+:~_-]+$ ]]; then
    echo "Invalid Debian version: $version" >&2
    exit 2
fi

for command_name in git python3 dpkg-deb tar gzip md5sum; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Missing build dependency: $command_name" >&2
        exit 2
    fi
done

output_dir="$repo_dir/dist"
output_file="$output_dir/terminator_${version}_all.deb"
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/terminator-deb.XXXXXXXX")
source_dir="$build_dir/source"
package_root="$build_dir/root"

cleanup() {
    rm -rf -- "$build_dir"
}
trap cleanup EXIT

mkdir -p "$source_dir" "$package_root" "$output_dir"
git -C "$repo_dir" archive --format=tar HEAD | tar -xf - -C "$source_dir"

source_date_epoch=$(git -C "$repo_dir" show -s --format=%ct HEAD)
export SOURCE_DATE_EPOCH=$source_date_epoch
export TZ=UTC
export LC_ALL=C.UTF-8
export PYTHONDONTWRITEBYTECODE=1

(
    cd "$source_dir"
    python3 setup.py --quiet build
    python3 setup.py --quiet install \
        --root="$package_root" \
        --prefix=/usr \
        --install-lib=/usr/lib/python3/dist-packages \
        --install-scripts=/usr/bin \
        --install-data=/usr \
        --single-version-externally-managed \
        --record="$build_dir/install-files.txt"
)

find "$package_root" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$package_root" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

install -d -m 0755 "$package_root/usr/share/doc/terminator"
install -m 0644 "$source_dir/AGENT_CONTROL.md" \
    "$package_root/usr/share/doc/terminator/AGENT_CONTROL.md"
install -m 0644 "$source_dir/REMOTINATOR_USAGE.md" \
    "$package_root/usr/share/doc/terminator/REMOTINATOR_USAGE.md"
install -m 0644 "$source_dir/AGENTS.md" \
    "$package_root/usr/share/doc/terminator/AGENTS.md"
install -m 0644 "$source_dir/COPYING" \
    "$package_root/usr/share/doc/terminator/copyright"

debian_date=$(date --date="@$source_date_epoch" --rfc-email)
sed -e "s/@VERSION@/$version/g" \
    -e "s/@DATE@/$debian_date/g" \
    "$script_dir/debian/changelog.Debian.in" \
    > "$package_root/usr/share/doc/terminator/changelog.Debian"
gzip -9n "$package_root/usr/share/doc/terminator/changelog.Debian"

while IFS= read -r -d '' manpage; do
    gzip -9n "$manpage"
done < <(find "$package_root/usr/share/man" -type f -print0)

install -d -m 0755 "$package_root/DEBIAN"
installed_size=$(du -sk "$package_root" | cut -f1)
sed -e "s/@VERSION@/$version/g" \
    -e "s/@INSTALLED_SIZE@/$installed_size/g" \
    "$script_dir/debian/control.in" \
    > "$package_root/DEBIAN/control"
install -m 0755 "$script_dir/debian/postinst" \
    "$package_root/DEBIAN/postinst"
install -m 0755 "$script_dir/debian/prerm" \
    "$package_root/DEBIAN/prerm"

# Do not let the invoking user's umask leak group-writable modes into the
# archive.  Python modules/data are regular files; only entry points and
# maintainer scripts need to be executable.
find "$package_root" -type d -exec chmod 0755 {} +
find "$package_root" -type f -exec chmod 0644 {} +
chmod 0755 \
    "$package_root/usr/bin/terminator" \
    "$package_root/usr/bin/remotinator" \
    "$package_root/DEBIAN/postinst" \
    "$package_root/DEBIAN/prerm"

(
    cd "$package_root"
    find usr -type f -print0 | sort -z | xargs -0 md5sum > DEBIAN/md5sums
)

find "$package_root" -print0 | xargs -0 touch --date="@$source_date_epoch"
dpkg-deb --root-owner-group --build "$package_root" "$output_file"

echo "$output_file"
