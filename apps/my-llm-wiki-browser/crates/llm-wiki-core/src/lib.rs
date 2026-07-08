pub mod config;
pub mod embedding;
pub mod error;
pub mod indexer;
pub mod models;
pub mod parser;
pub mod paths;
pub mod registry;
pub mod search;

pub use embedding::{Chunk, EmbeddedChunk, EmbeddingProvider, VectorHit, VectorStore};
pub use error::{Error, Result};
pub use indexer::{IndexManager, WikiIndex, to_plain_text};
pub use models::{PageRecord, SearchResult, WikiEntry, WikiSummary};
pub use search::{FullTextSearcher, SearchDoc};
