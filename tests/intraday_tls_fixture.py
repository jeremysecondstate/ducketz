"""Socket-free tests may model a native server without changing client TLS.

The production listener must use its own supported interpreter environment.
This helper loads the installed stdlib source in a separate namespace solely
for an explicit offline peer. It never extracts or changes an injected context.
"""
import ast
import _ssl
from pathlib import Path
import ssl
import sys
from types import SimpleNamespace


def native_ssl_module(monkeypatch):
    original_module, original_context = sys.modules['ssl'], ssl.SSLContext
    # Execute the exact installed stdlib native class in a separate namespace.
    # Loading the whole module would execute enum._convert_ against sys.modules;
    # this deliberately executes no imports, enum conversion or module setup.
    tree = ast.parse(Path(ssl.__file__).read_text(encoding='utf-8'))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'SSLContext']
    assert len(classes) == 1 and not classes[0].decorator_list
    assert len(classes[0].bases) == 1
    assert ast.dump(classes[0].bases[0]) == ast.dump(ast.parse('_SSLContext', mode='eval').body)
    namespace = dict(vars(ssl))
    assert namespace['_SSLContext'] is _ssl._SSLContext
    exec(compile(ast.Module(body=classes, type_ignores=[]), ssl.__file__, 'exec'), namespace)
    # These are the two class wiring assignments at the end of stdlib ssl.py.
    namespace['SSLContext'].sslsocket_class = namespace['SSLSocket']
    namespace['SSLContext'].sslobject_class = namespace['SSLObject']
    native = SimpleNamespace(**namespace)
    assert sys.modules['ssl'] is original_module and ssl.SSLContext is original_context
    return native


def native_server_context(monkeypatch, *, certificate, private_key, client_ca):
    from ml.intraday import admission_transport as transport
    native = native_ssl_module(monkeypatch)
    original_context = ssl.SSLContext
    with monkeypatch.context() as patch:
        patch.setattr(transport, 'ssl', native)
        result = transport.server_tls_context(certificate=certificate,
            private_key=private_key, client_ca=client_ca)
    assert ssl.SSLContext is original_context
    return result


def handshake(client, server, hostname='atlas.test'):
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client_ssl = client.wrap_bio(client_in, client_out, server_hostname=hostname)
    server_ssl = server.wrap_bio(server_in, server_out, server_side=True)
    complete = set()
    for _ in range(20):
        for name, connection in (('client', client_ssl), ('server', server_ssl)):
            try:
                connection.do_handshake()
                complete.add(name)
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLError as error:
                error.peer_side = name
                raise
        for source, target in ((client_out, server_in), (server_out, client_in)):
            if source.pending:
                target.write(source.read())
        if len(complete) == 2:
            return client_ssl, server_ssl
    raise AssertionError('Offline mutual TLS handshake did not complete')
