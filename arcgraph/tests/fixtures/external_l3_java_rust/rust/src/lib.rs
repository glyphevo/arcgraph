pub mod service {
    pub trait Saver {
        fn save(&self, value: &str) -> String;
    }

    pub struct Repository {
        pub name: String,
    }

    impl Saver for Repository {
        fn save(&self, value: &str) -> String {
            format!("{}:{value}", self.name)
        }
    }

    pub fn use_saver(repo: &Repository, value: &str) -> String {
        repo.save(value)
    }

    #[cfg(feature = "dynamic")]
    pub fn cfg_only(repo: &dyn Saver, value: &str) -> String {
        repo.save(value)
    }
}
