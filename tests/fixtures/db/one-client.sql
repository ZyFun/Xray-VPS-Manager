INSERT INTO manager_metadata(key, value, updated_at) VALUES ('jsonImport.completed', 'true', '2026-10-06T12:00:00Z');
INSERT INTO reality_connections(tag,name,port,sni,dest,fingerprint,public_key,short_id,created_at)
VALUES ('vless-reality','Reality',443,'www.example.com','www.example.com:443','chrome','AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA','0123456789abcdef','2026-10-06T12:00:00Z');
INSERT INTO clients(name,uuid,email,connection_tag,created_at,enabled,payment_type,xray_client_json)
VALUES ('alice','00000000-0000-4000-8000-000000000001','alice|created=2026-10-06T12:00:00Z|connection=vless-reality','vless-reality','2026-10-06T12:00:00Z',1,'free','{"id":"00000000-0000-4000-8000-000000000001","flow":"xtls-rprx-vision","email":"alice|created=2026-10-06T12:00:00Z|connection=vless-reality"}');
INSERT INTO client_credentials(client_name,connection_tag,credential_uuid,protocol,security,transport,enabled,created_at,xray_client_json)
VALUES ('alice','vless-reality','00000000-0000-4000-8000-000000000001','vless','reality','tcp',1,'2026-10-06T12:00:00Z','{"id":"00000000-0000-4000-8000-000000000001","flow":"xtls-rprx-vision","email":"alice|created=2026-10-06T12:00:00Z|connection=vless-reality"}');
