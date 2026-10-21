package fixture;

public class AccountService {
    private final AccountRepository repository;

    public AccountService(AccountRepository repository) {
        this.repository = repository;
    }

    public int withdraw(int amount) {
        return repository.withdraw(amount);
    }
}
