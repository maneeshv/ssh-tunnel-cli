//! Test-only lifecycle driver: authentication keeps the real controlling terminal.
use ssh_tunnel_cli_rs::core::{Manager, Settings, TunnelError};
use std::io::{self, Write};
use std::path::PathBuf;

fn run() -> Result<(), TunnelError> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let manager = Manager::new(Some(PathBuf::from(&args[1])), Some(PathBuf::from(&args[2])))?;
    if args[0] != "start" {
        let output = match args[0].as_str() {
            "records" => serde_json::to_string(&manager.records()?),
            "stop" => serde_json::to_string(&manager.stop(&args[3])?),
            "logs" => serde_json::to_string(&manager.logs(&args[3])?),
            _ => return Err(TunnelError::Message("Unknown probe operation".into())),
        };
        println!("{}", output.unwrap());
        return Ok(());
    }
    let record = manager.prepare(Settings {
        alias: "fixture".into(),
        remote_port: args[3].parse().unwrap(),
        local_port: args[4].parse().unwrap(),
        destination: "127.0.0.1".into(),
        mode: args[5].clone(),
    })?;
    println!("PREPARED:{}", record.id);
    print!("START? ");
    io::stdout().flush().unwrap();
    let mut answer = String::new();
    io::stdin().read_line(&mut answer).unwrap();
    let result = manager.start(record, |started| {
        println!("RUNNING:{}", started.id);
        io::stdout().flush().unwrap();
        if args[6] == "callback_error" {
            Err(TunnelError::Message("test callback failure".into()))
        } else {
            Ok(())
        }
    });
    match result {
        Ok(record) => {
            println!("RETURNED:{}", record.status);
            if args[6] == "hold_after_stop" {
                manager.stop(&record.id)?;
                println!("STOPPED");
            }
            if args[6] == "hold_after_start" || args[6] == "hold_after_stop" {
                println!("HOLDING");
                io::stdout().flush().unwrap();
                io::stdin().read_line(&mut String::new()).unwrap();
            }
        }
        Err(error) if error.is_cancelled() => {
            println!("CANCELLED:{error}");
            std::process::exit(130);
        }
        Err(error) if args[6] == "callback_error" || args[6] == "expected_error" => {
            println!("EXPECTED_ERROR:{error}");
        }
        Err(error) => return Err(error),
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(if error.is_cancelled() { 130 } else { 1 });
    }
}
