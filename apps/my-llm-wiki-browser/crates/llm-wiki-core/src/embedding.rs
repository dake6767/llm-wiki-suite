use serde::{Deserialize, Serialize};

use crate::Result;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Chunk {
    pub wiki: String,
    pub page_path: String,
    pub chunk_id: String,
    pub text: String,
    pub content_hash: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EmbeddedChunk {
    pub chunk: Chunk,
    pub vector: Vec<f32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct VectorHit {
    pub wiki: String,
    pub page_path: String,
    pub chunk_id: String,
    pub score: f32,
    pub text: String,
}

pub trait EmbeddingProvider: Send + Sync {
    fn embed_documents<'a>(
        &'a self,
        chunks: &'a [Chunk],
    ) -> impl std::future::Future<Output = Result<Vec<EmbeddedChunk>>> + Send + 'a;

    fn embed_query<'a>(
        &'a self,
        query: &'a str,
    ) -> impl std::future::Future<Output = Result<Vec<f32>>> + Send + 'a;
}

pub trait VectorStore: Send + Sync {
    fn upsert<'a>(
        &'a self,
        chunks: &'a [EmbeddedChunk],
    ) -> impl std::future::Future<Output = Result<()>> + Send + 'a;

    fn search<'a>(
        &'a self,
        wiki: &'a str,
        vector: &'a [f32],
        limit: usize,
    ) -> impl std::future::Future<Output = Result<Vec<VectorHit>>> + Send + 'a;
}
