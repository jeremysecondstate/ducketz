"""Offline synthetic certificates and private binding conversion only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ml.intraday import admission_enrollment as enrollment, admission_transport as transport, scout_enrollment
from ml.intraday.scout_authority import ScoutAuthorityClient, load_binding
from ml.intraday.scout_books import ScoutBooks
from test_intraday_scout_authority import readiness_response
from test_intraday_admission_transport import FakeResponse

NOW = datetime(2026, 10, 13, 6, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        pytest.fail('Enrollment fixture must never use a socket')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'bind', forbidden)


@pytest.fixture
def accepted(tmp_path, monkeypatch):
    # Published enrollment tests cover real owner-only ACL establishment.
    # The certificates, private keys and immutable artifact bytes here are real
    # synthetic fixture data, never a production enrollment or endpoint.
    monkeypatch.setattr(enrollment, '_private_permissions', lambda *a, **k: None)
    atlas, scout = tmp_path/'atlas', tmp_path/'scout'
    symbols = ['S'+str(i) for i in range(11)]
    offer = enrollment.create_offer(atlas, enrollment_id='1'*32, server_identity='atlas-fixture',
        endpoint='https://atlas.test:9443', account_fingerprint='a'*64, symbols=symbols, now=NOW)
    request = enrollment.create_request(scout, offer, trusted_offer_sha256=enrollment.payload_sha256(offer),
        expected_server_identity=offer['server_identity'], account_fingerprint=offer['account_fingerprint'], symbols=symbols, now=NOW)
    response = enrollment.sign_request(atlas, request, now=NOW)
    v1 = enrollment.accept_response(scout, response, now=NOW)
    receipt = dict(kind='admission_offer', enrollment_id=offer['enrollment_id'], action_date='2026-10-13',
        review_session='2026-10-12', packet_sha256='f'*64, payload=offer,
        trusted_payload_sha256=enrollment.payload_sha256(offer))
    receipt_path=tmp_path/'trusted-offer-receipt.json'
    receipt_path.write_bytes(enrollment.encoded(receipt))
    options=dict(directory=scout, offer_receipt_path=receipt_path,
        offer_receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        enrollment_id=offer['enrollment_id'], action_date=receipt['action_date'], review_session=receipt['review_session'],
        server_identity=offer['server_identity'], account_fingerprint=offer['account_fingerprint'], symbols=symbols,
        destination=scout/'scout-binding-v2.json', now=NOW)
    return SimpleNamespace(atlas=atlas, scout=scout, offer=offer, request=request, response=response,
        receipt=receipt, options=options, v1=v1)


def snapshot(directory):
    return {p.name:p.read_bytes() for p in directory.iterdir() if p.name!='enrollment.lock' and p.is_file()}


def convert(accepted, **changes):
    return scout_enrollment.construct_v2_binding(**(accepted.options | changes))


def test_separate_v2_binding_preserves_every_accepted_artifact_and_key(accepted):
    before=snapshot(accepted.scout)
    binding=convert(accepted)
    expected=dict(accepted.v1, version='scout-atlas-authority-binding-v2',
        server_certificate_sha256=accepted.offer['server_sha256'])
    assert binding.payload()==expected
    assert load_binding(accepted.options['destination'])==binding
    assert accepted.offer['server_sha256'] != hashlib.sha256(accepted.offer['server_certificate'].encode()).hexdigest()
    assert all((accepted.scout/name).read_bytes()==raw for name,raw in before.items())
    after=snapshot(accepted.scout)
    assert convert(accepted)==binding and snapshot(accepted.scout)==after
    assert (accepted.scout/'scout-binding.json').read_bytes()==enrollment.encoded(accepted.v1)


@pytest.mark.parametrize('field,value', [('offer_receipt_sha256','0'*64),('enrollment_id','2'*32),
    ('action_date','2026-10-14'),('review_session','2026-10-11'),('server_identity','foreign'),
    ('account_fingerprint','b'*64),('symbols',['Z'+str(i) for i in range(11)])])
def test_independent_local_trust_identity_cannot_be_replaced(accepted, field, value):
    before=snapshot(accepted.scout)
    with pytest.raises(ValueError): convert(accepted, **{field:value})
    assert snapshot(accepted.scout)==before


@pytest.mark.parametrize('damage',['kind','digest','packet','extra','foreign_payload','duplicate'])
def test_retained_receipt_requires_exact_envelope_and_accepted_original(accepted, damage):
    value=deepcopy(accepted.receipt)
    if damage=='kind': value['kind']='admission_cert'
    elif damage=='digest': value['trusted_payload_sha256']='0'*64
    elif damage=='packet': value['packet_sha256']='not-a-digest'
    elif damage=='extra': value['extra']=True
    elif damage=='foreign_payload':
        value['payload']['nonce']='0'*64
        value['trusted_payload_sha256']=enrollment.payload_sha256(value['payload'])
    raw=enrollment.encoded(value)
    if damage=='duplicate': raw=raw.rstrip()[:-1]+b',"kind":"admission_offer"}\n'
    accepted.options['offer_receipt_path'].write_bytes(raw)
    accepted.options['offer_receipt_sha256']=hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError): convert(accepted)
    assert not accepted.options['destination'].exists()


@pytest.mark.parametrize('name',['offer.json','request.json','response.json','scout-binding.json','ca.pem','client.pem','client.key'])
def test_missing_accepted_files_are_not_recreated(accepted,name):
    (accepted.scout/name).unlink()
    before=snapshot(accepted.scout)
    with pytest.raises((OSError,ValueError)): convert(accepted)
    assert snapshot(accepted.scout)==before


@pytest.mark.parametrize('name',['offer.json','request.json','response.json','scout-binding.json','ca.pem','client.pem','client.key'])
def test_mutated_accepted_files_are_not_repaired(accepted,name):
    path=accepted.scout/name
    if name=='client.key': path.write_bytes((accepted.atlas/'server.key').read_bytes())
    elif name.endswith('.pem'): path.write_bytes(path.read_bytes()+b'changed')
    else:
        value=json.loads(path.read_bytes())
        value['extra']=True
        path.write_bytes(enrollment.encoded(value))
    before=snapshot(accepted.scout)
    with pytest.raises((OSError,ValueError)): convert(accepted)
    assert snapshot(accepted.scout)==before


@pytest.mark.parametrize('name',['offer.json','request.json','response.json','scout-binding.json','client.pem','ca.pem','client.key','enrollment.lock'])
def test_conversion_cannot_target_original_artifacts(accepted,name):
    before=snapshot(accepted.scout)
    with pytest.raises(ValueError): convert(accepted,destination=accepted.scout/name)
    assert snapshot(accepted.scout)==before


def test_conversion_cannot_target_retained_receipt(accepted):
    before=accepted.options['offer_receipt_path'].read_bytes()
    with pytest.raises(ValueError): convert(accepted,destination=accepted.options['offer_receipt_path'])
    assert accepted.options['offer_receipt_path'].read_bytes()==before


def test_offer_expiry_is_historical_only_after_accepted_v1_and_certificates_still_expire(accepted):
    assert convert(accepted,now=NOW+timedelta(days=2)).server_certificate_sha256==accepted.offer['server_sha256']
    before=snapshot(accepted.scout)
    with pytest.raises(ValueError): convert(accepted,now=NOW+timedelta(days=31))
    with pytest.raises(ValueError): convert(accepted,now=NOW-timedelta(seconds=1))
    assert snapshot(accepted.scout)==before


def test_wrong_existing_destination_or_interrupted_write_never_overwrites(accepted):
    path=accepted.options['destination']
    path.write_bytes(b'{"partial":')
    with pytest.raises(ValueError): convert(accepted)
    assert path.read_bytes()==b'{"partial":'


def test_change_during_key_validation_is_caught_before_binding_write(accepted,monkeypatch):
    real=enrollment._key
    def mutation(path):
        key=real(path)
        (accepted.scout/'client.pem').write_bytes(b'changed after initial read')
        return key
    monkeypatch.setattr(enrollment,'_key',mutation)
    with pytest.raises(ValueError,match='changed during conversion'): convert(accepted)
    assert not accepted.options['destination'].exists()


def test_key_replacement_after_validation_is_caught_before_binding_write(accepted,monkeypatch):
    real=enrollment._key
    def mutation(path):
        key=real(path)
        Path(path).write_bytes((accepted.atlas/'server.key').read_bytes())
        return key
    monkeypatch.setattr(enrollment,'_key',mutation)
    with pytest.raises(ValueError,match='changed during conversion'): convert(accepted)
    assert not accepted.options['destination'].exists()


def test_no_absent_directory_or_implicit_key_generation(accepted,tmp_path,monkeypatch):
    for name in ('create_offer','create_request','sign_request','accept_response','_new_key'):
        monkeypatch.setattr(enrollment,name,lambda *a,**k: pytest.fail('No enrollment effect allowed'))
    assert convert(accepted)
    target=tmp_path/'absent'
    with pytest.raises(ValueError): convert(accepted,directory=target)
    assert not target.exists()


@pytest.mark.parametrize('mismatch',[False,True])
def test_actual_published_transport_and_scout_client_pin_leaf_before_request(accepted,tmp_path,monkeypatch,mismatch):
    from cryptography.hazmat.primitives.serialization import Encoding
    binding=convert(accepted)
    der=enrollment._cert(accepted.offer['server_certificate']).public_bytes(Encoding.DER)
    events=[]
    response=[None]
    class Connection:
        def __init__(self,*args,**kwargs):
            self.sock=SimpleNamespace(getpeercert=lambda binary_form: b'other-leaf' if mismatch else der)
            transport._validate_context(kwargs['context'],server=False)
        def connect(self): events.append('tls')
        def request(self,method,path,body,headers):
            events.append('request')
            assert method=='POST' and path=='/v1/readiness'
            nonce=json.loads(body)['request_nonce']
            response[0]=FakeResponse(raw=json.dumps(readiness_response(binding,NOW,nonce)).encode())
        def getresponse(self): return response[0]
        def close(self): events.append('closed')
    monkeypatch.setattr(transport.http.client,'HTTPSConnection',Connection)
    books=ScoutBooks(tmp_path/'books',account_fingerprint=binding.account_fingerprint)
    client=ScoutAuthorityClient(binding,books,clock=lambda:NOW)
    if mismatch:
        with pytest.raises(transport.AdmissionTransportError): client.readiness()
        assert events==['tls','closed'] and not books.root.exists()
    else:
        assert client.readiness().response['ready'] is True
        assert events==['tls','request','closed']


def test_cli_redacts_private_fields_and_forbids_injected_clock(accepted,tmp_path,capsys):
    params={k:str(v) if isinstance(v,Path) else v for k,v in accepted.options.items() if k!='now'}
    path=tmp_path/'parameters.json'
    path.write_bytes(enrollment.encoded(dict(params,now='fake-clock')))
    assert scout_enrollment.main(['--input',str(path)])==1
    assert json.loads(capsys.readouterr().out)['error']=='scout_binding_conversion_failed'
    path.write_bytes(enrollment.encoded(dict(params,server_identity='private-incorrect-server')))
    assert scout_enrollment.main(['--input',str(path)])==1
    result=capsys.readouterr()
    assert result.err=='' and 'private-incorrect-server' not in result.out and 'https://' not in result.out


def test_cli_success_is_inert_and_prints_only_safe_status(accepted,tmp_path,capsys,monkeypatch):
    parameters={k:str(v) if isinstance(v,Path) else v for k,v in accepted.options.items() if k!='now'}
    path=tmp_path/'parameters.json'
    path.write_bytes(enrollment.encoded(parameters))
    real=enrollment._now
    monkeypatch.setattr(enrollment,'_now',lambda value: real(NOW if value is None else value))
    assert scout_enrollment.main(['--input',str(path)])==0
    assert json.loads(capsys.readouterr().out)==dict(status='SCOUT_V2_BINDING_PREPARED',orders_or_commitments_created=0)
    assert load_binding(accepted.options['destination']).server_certificate_sha256==accepted.offer['server_sha256']
