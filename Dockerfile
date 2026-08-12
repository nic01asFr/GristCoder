FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# esbuild — binaire Go statique (ni Node, ni npm, ni libc dynamique). Sert a
# bundler les artefacts qui importent des paquets npm : sans lui, un artefact
# contenant des `import` produit un widget blanc.
#   ~11,4 Mo sur l'image. Extrait du tarball npm, seul canal de distribution
#   officiel du binaire (les releases GitHub d'esbuild n'ont aucun asset).
# Recupere ici plutot qu'au demarrage : pas de dependance reseau au boot, et
# la version est figee avec l'image.
ARG ESBUILD_VERSION=0.28.2
RUN python -c "\
import urllib.request, tarfile, io, os, hashlib, sys; \
v = os.environ.get('ESBUILD_VERSION', '${ESBUILD_VERSION}'); \
u = f'https://registry.npmjs.org/@esbuild/linux-x64/-/linux-x64-{v}.tgz'; \
d = urllib.request.urlopen(u, timeout=120).read(); \
print('esbuild tarball', len(d), 'o sha256', hashlib.sha256(d).hexdigest()[:16]); \
b = tarfile.open(fileobj=io.BytesIO(d)).extractfile('package/bin/esbuild').read(); \
open('/usr/local/bin/esbuild','wb').write(b); \
os.chmod('/usr/local/bin/esbuild', 0o755); \
print('esbuild binaire', len(b), 'o')" \
 && esbuild --version

COPY grist_coder.py .
COPY widget.html .
COPY harness/ ./harness/

# Port du service (surchargable via $PORT, injecte par le chart Onyxia)
EXPOSE 8742

CMD ["sh", "-c", "uvicorn grist_coder:app --host 0.0.0.0 --port ${PORT:-8742}"]
