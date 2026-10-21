package fixture;

public class OrderRepository {
    private int saved;
    public void save(String orderId) { saved++; }
    public int savedCount() { return saved; }
}
