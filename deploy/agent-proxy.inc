limit_conn agent_connections 20;
limit_req zone=agent_requests burst=40 nodelay;

proxy_pass_request_headers off;
proxy_set_header Host 127.0.0.1:8080;
proxy_set_header Authorization $http_authorization;
proxy_set_header Content-Type $http_content_type;
proxy_set_header Accept $http_accept;
proxy_set_header X-Request-ID $http_x_request_id;
proxy_set_header X-Kitsune-Agent-Remote $remote_addr;
proxy_set_header Connection "";
proxy_http_version 1.1;
proxy_connect_timeout 3s;
proxy_read_timeout 30s;
proxy_pass http://127.0.0.1:8080;
