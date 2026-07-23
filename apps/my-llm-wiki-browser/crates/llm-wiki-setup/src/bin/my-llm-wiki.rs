use std::collections::BTreeSet;
use std::path::PathBuf;

use clap::{Args, Parser, Subcommand};
use llm_wiki_setup::{McpBridgeOptions, SetupCore, SetupRequest, run_mcp_bridge};
use serde::Serialize;

#[derive(Parser)]
#[command(
    name = "my-llm-wiki",
    version,
    about = "My LLM Wiki setup and maintenance"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Inspect(OutputArgs),
    Setup(SetupArgs),
    Status(OutputArgs),
    Repair(OutputArgs),
    EnsurePack(EnsurePackArgs),
    Update(UpdateArgs),
    Uninstall(UninstallArgs),
    McpBridge(McpBridgeArgs),
}

#[derive(Args)]
struct OutputArgs {
    #[arg(long)]
    json: bool,
}

#[derive(Args)]
struct SetupArgs {
    #[arg(long = "host", required = true)]
    hosts: Vec<String>,
    #[arg(long = "replace", value_name = "EXACT_PATH")]
    replace: Vec<PathBuf>,
    #[arg(long, value_name = "PATH")]
    wiki_path: Option<PathBuf>,
    #[arg(long)]
    without_toolchain: bool,
    #[arg(long)]
    json: bool,
}

#[derive(Args)]
struct UpdateArgs {
    #[arg(long)]
    check: bool,
    #[arg(long)]
    json: bool,
}

#[derive(Args)]
struct EnsurePackArgs {
    id: String,
    #[arg(long)]
    json: bool,
}

#[derive(Args)]
struct UninstallArgs {
    #[arg(long = "host", required_unless_present = "all")]
    hosts: Vec<String>,
    #[arg(long, conflicts_with = "hosts")]
    all: bool,
    #[arg(long)]
    json: bool,
}

#[derive(Args)]
struct McpBridgeArgs {
    #[arg(long)]
    endpoint: Option<String>,
    #[arg(long)]
    token_file: Option<PathBuf>,
    #[arg(long)]
    probe: bool,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("my-llm-wiki: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    let core = SetupCore::from_environment()?;
    match cli.command {
        Command::Inspect(args) => print(&core.inspect()?, args.json)?,
        Command::Setup(args) => print(
            &core.setup(SetupRequest {
                hosts: args.hosts.into_iter().collect(),
                replace: args.replace.into_iter().collect(),
                install_official_toolchain: !args.without_toolchain,
                wiki_path: args.wiki_path,
            })?,
            args.json,
        )?,
        Command::Status(args) => print(&core.status()?, args.json)?,
        Command::Repair(args) => print(&core.repair()?, args.json)?,
        Command::EnsurePack(args) => print(&core.ensure_pack(&args.id)?, args.json)?,
        Command::Update(args) => {
            print(&core.update(args.check)?, args.json)?;
        }
        Command::Uninstall(args) => print(
            &core.uninstall(&args.hosts.into_iter().collect::<BTreeSet<_>>(), args.all)?,
            args.json,
        )?,
        Command::McpBridge(args) => run_mcp_bridge(McpBridgeOptions {
            endpoint: args.endpoint,
            token_file: args.token_file,
            probe: args.probe,
        })
        .map_err(std::io::Error::other)?,
    }
    Ok(())
}

fn print<T: Serialize + std::fmt::Debug>(value: &T, json: bool) -> Result<(), serde_json::Error> {
    if json {
        println!("{}", serde_json::to_string_pretty(value)?);
    } else {
        println!("{value:#?}");
    }
    Ok(())
}
