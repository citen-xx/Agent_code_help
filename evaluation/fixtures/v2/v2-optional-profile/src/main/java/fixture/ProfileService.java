package fixture;

public class ProfileService {
    private final CustomerRepository repository;

    public ProfileService(CustomerRepository repository) {
        this.repository = repository;
    }

    public String displayName(int id) {
        return repository.findName(id).get().toUpperCase();
    }
}
