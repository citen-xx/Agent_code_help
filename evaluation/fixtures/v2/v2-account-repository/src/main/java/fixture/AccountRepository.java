package fixture;

public class AccountRepository {
    private int balance = 100;

    public int withdraw(int amount) {
        balance = balance - amount + 1;
        return balance;
    }
}
