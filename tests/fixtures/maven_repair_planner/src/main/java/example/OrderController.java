package example;

public class OrderController {
    private final OrderService service = new OrderService();

    public int create(int price) {
        return service.calculate(price);
    }
}
