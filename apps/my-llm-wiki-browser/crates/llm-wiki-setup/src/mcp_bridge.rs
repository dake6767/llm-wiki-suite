use std::io::{BufRead as _, Read as _, Write};
use std::net::IpAddr;
use std::path::Path;
use std::path::PathBuf;
use std::time::Duration;

use reqwest::blocking::Client;
use serde_json::{Value, json};

const MAX_RESPONSE_BYTES: u64 = 16 * 1024 * 1024;

pub struct McpBridgeOptions {
    pub endpoint: Option<String>,
    pub token_file: Option<PathBuf>,
    pub probe: bool,
}

pub fn run(options: McpBridgeOptions) -> std::result::Result<(), String> {
    let home = dirs::home_dir().ok_or_else(|| "home directory is unavailable".to_owned())?;
    let connector = home.join(".my-llm-wiki/connector");
    let endpoint = options.endpoint.unwrap_or_else(|| {
        let port = std::fs::read_to_string(connector.join("server-port"))
            .ok()
            .and_then(|value| value.trim().parse::<u16>().ok())
            .filter(|value| *value >= 1024)
            .or_else(|| {
                std::env::var("LLM_WIKI_PORT")
                    .ok()
                    .and_then(|value| value.parse::<u16>().ok())
                    .filter(|value| *value >= 1024)
            })
            .unwrap_or(8800);
        format!("http://127.0.0.1:{port}/mcp")
    });
    validate_endpoint(&endpoint)?;
    let token_path = options
        .token_file
        .unwrap_or_else(|| connector.join("token"));
    let client = Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(30))
        .user_agent(format!(
            "my-llm-wiki-mcp-bridge/{}",
            env!("CARGO_PKG_VERSION")
        ))
        .build()
        .map_err(|error| error.to_string())?;

    if options.probe {
        let payload = json!({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}});
        let (_, reply) = post(&client, &endpoint, &token_path, &payload)?;
        println!(
            "{}",
            serde_json::to_string(&reply).map_err(|error| error.to_string())?
        );
        return Ok(());
    }

    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout().lock();
    for line in stdin.lock().lines() {
        let line = line.map_err(|error| error.to_string())?;
        if line.trim().is_empty() {
            continue;
        }
        let request: Value = match serde_json::from_str(&line) {
            Ok(value) => value,
            Err(error) => {
                write_reply(
                    &mut stdout,
                    &json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":error.to_string()}}),
                )?;
                continue;
            }
        };
        let id = request.get("id").cloned();
        match post(&client, &endpoint, &token_path, &request) {
            Ok((202, _)) => {}
            Ok((_, reply)) if id.is_some() => write_reply(&mut stdout, &reply)?,
            Ok(_) => {}
            Err(error) if id.is_some() => write_reply(
                &mut stdout,
                &json!({"jsonrpc":"2.0","id":id,"error":{"code":-32000,"message":error}}),
            )?,
            Err(error) => eprintln!("my-llm-wiki mcp-bridge: {error}"),
        }
    }
    Ok(())
}

fn post(
    client: &Client,
    endpoint: &str,
    token_path: &Path,
    payload: &Value,
) -> std::result::Result<(u16, Value), String> {
    let mut request = client
        .post(endpoint)
        .header("Accept", "application/json, text/event-stream")
        .json(payload);
    if let Ok(token) = std::fs::read_to_string(token_path)
        && !token.trim().is_empty()
    {
        request = request.bearer_auth(token.trim());
    }
    let response = request.send().map_err(|error| error.to_string())?;
    let status = response.status().as_u16();
    if status == 202 {
        return Ok((status, Value::Null));
    }
    let mut body = Vec::new();
    response
        .take(MAX_RESPONSE_BYTES + 1)
        .read_to_end(&mut body)
        .map_err(|error| error.to_string())?;
    if body.len() as u64 > MAX_RESPONSE_BYTES {
        return Err(format!(
            "HTTP {status} response exceeds {MAX_RESPONSE_BYTES} bytes"
        ));
    }
    let body = String::from_utf8_lossy(&body);
    if !(200..300).contains(&status) {
        return Err(format!("HTTP {status}: {body}"));
    }
    let value = serde_json::from_str(&body)
        .map_err(|error| format!("HTTP {status} returned invalid JSON: {error}"))?;
    Ok((status, value))
}

fn validate_endpoint(endpoint: &str) -> std::result::Result<(), String> {
    let url =
        reqwest::Url::parse(endpoint).map_err(|error| format!("invalid MCP endpoint: {error}"))?;
    let host = url
        .host_str()
        .ok_or_else(|| "MCP endpoint has no host".to_owned())?;
    let address_text = host
        .strip_prefix('[')
        .and_then(|value| value.strip_suffix(']'))
        .unwrap_or(host);
    let loopback = host.eq_ignore_ascii_case("localhost")
        || address_text
            .parse::<IpAddr>()
            .is_ok_and(|address| address.is_loopback());
    if url.scheme() != "http"
        || !loopback
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(
            "MCP endpoint must be a credential-free loopback HTTP URL without query or fragment"
                .into(),
        );
    }
    Ok(())
}

fn write_reply(output: &mut impl Write, reply: &Value) -> std::result::Result<(), String> {
    serde_json::to_writer(&mut *output, reply).map_err(|error| error.to_string())?;
    output.write_all(b"\n").map_err(|error| error.to_string())?;
    output.flush().map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::validate_endpoint;

    #[test]
    fn bridge_only_sends_the_local_token_to_loopback_http() {
        assert!(validate_endpoint("http://127.0.0.1:8800/mcp").is_ok());
        assert!(validate_endpoint("http://[::1]:8800/mcp").is_ok());
        assert!(validate_endpoint("http://localhost:8800/mcp").is_ok());
        assert!(validate_endpoint("https://example.com/mcp").is_err());
        assert!(validate_endpoint("http://127.0.0.1:8800/mcp?token=x").is_err());
        assert!(validate_endpoint("http://user@127.0.0.1:8800/mcp").is_err());
    }
}
